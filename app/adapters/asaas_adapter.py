"""
Asaas Payment Gateway Adapter.
Implements PaymentGatewayPort for Asaas BaaS API integration.
Includes resilience patterns: retry, timeout, circuit breaker.
"""
import httpx
import re
import pyotp
from typing import Dict, Any, Optional
from decimal import Decimal
from datetime import datetime, timedelta
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)
import logging

from app.ports.payment_gateway_port import PaymentGatewayPort
from app.core.logger import logger
from app.core.security import mask_sensitive_data


class AsaasAdapter(PaymentGatewayPort):
    """
    Asaas API adapter with production-grade resilience.

    Features:
    - Idempotency: all operations support idempotency keys
    - Retry: automatic retry with exponential backoff
    - Timeout: 15s timeout per request
    - Circuit Breaker: fail fast after 3 consecutive failures
    - Observability: structured logging with masked credentials
    """

    BASE_URL_PRODUCTION = "https://api.asaas.com/v3"
    BASE_URL_SANDBOX = "https://sandbox.asaas.com/api/v3"

    def __init__(self, api_key: str, use_sandbox: bool = False, operation_key: Optional[str] = None, totp_secret: Optional[str] = None):
        """
        Initializes Asaas adapter.

        Args:
            api_key: Asaas API key ($aact_prod_... or $aact_sandbox_...)
            use_sandbox: If True, use sandbox environment
            operation_key: Static operation key (used only when totp_secret is absent)
            totp_secret: Base32 TOTP secret from Asaas device authorization setup.
                         When present, a fresh 6-digit TOTP code is generated at every
                         transfer call and sent as operationKey, fully replacing the
                         static key. Obtain it from:
                         Asaas > Configuracoes > Seguranca > Autorizacao por dispositivo.
        """
        if not api_key:
            raise ValueError("Asaas API key is required")

        self.api_key = api_key
        self.operation_key = operation_key
        self.totp_secret = totp_secret.strip() if totp_secret else None
        self.base_url = self.BASE_URL_SANDBOX if use_sandbox else self.BASE_URL_PRODUCTION

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "access_token": api_key,
                "Content-Type": "application/json",
                "User-Agent": "BioCodeTechPay/1.0"
            },
            timeout=15.0  # 15 seconds timeout
        )

        if self.totp_secret:
            logger.info(
                "Asaas adapter: TOTP device authorization active. "
                "operationKey will be generated dynamically at each transfer."
            )
        elif not self.operation_key:
            logger.warning(
                "ASAAS_OPERATION_KEY and ASAAS_TOTP_SECRET are both unset. "
                "Transfers will require manual authorization in Asaas Dashboard. "
                "Set ASAAS_TOTP_SECRET (recommended) or ASAAS_OPERATION_KEY in Render."
            )

        logger.info(
            f"Asaas adapter initialized: environment={'sandbox' if use_sandbox else 'production'}, "
            f"key={mask_sensitive_data(api_key, visible_chars=12)}, "
            f"auth={'totp' if self.totp_secret else ('static_key' if self.operation_key else 'NONE')}"
        )

    def __del__(self):
        """Cleanup HTTP client on destruction."""
        if hasattr(self, 'client'):
            self.client.close()

    def _get_operation_key(self) -> Optional[str]:
        """
        Returns the appropriate operationKey for the current request.

        Priority: TOTP (dynamic) > static operation_key > None.

        TOTP codes are generated fresh at call time using pyotp.TOTP.now().
        Each code is valid for 30 seconds; pyotp automatically derives the
        correct window from the system clock synchronized with the TOTP secret.
        """
        if self.totp_secret:
            try:
                code = pyotp.TOTP(self.totp_secret).now()
                logger.info("operationKey: TOTP code generated successfully (valid 30s window)")
                return code
            except Exception as e:
                logger.error(f"TOTP generation failed: {e}. Falling back to static key if available.")
        return self.operation_key or None

    def __del__(self):
        """Cleanup HTTP client on destruction."""
        if hasattr(self, 'client'):
            self.client.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Makes HTTP request to Asaas API with resilience patterns.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (e.g., "/payments")
            data: Request body (JSON)
            params: Query parameters
            idempotency_key: Idempotency key header

        Returns:
            Parsed JSON response

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx errors
            httpx.TimeoutException: On timeout
        """
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = self.client.request(
                method=method,
                url=endpoint,
                json=data,
                params=params,
                headers=headers
            )

            response.raise_for_status()

            logger.info(
                f"Asaas API success: {method} {endpoint} -> {response.status_code}",
                extra={"status_code": response.status_code, "endpoint": endpoint}
            )

            return response.json() if response.text else {}

        except httpx.HTTPStatusError as e:
            error_detail = e.response.text if e.response else "unknown"
            logger.error(
                f"Asaas API error: {method} {endpoint} -> {e.response.status_code}: {error_detail}",
                extra={"status_code": e.response.status_code, "error": error_detail}
            )
            raise
        except httpx.TimeoutException as e:
            logger.error(f"Asaas API timeout: {method} {endpoint}")
            raise
        except Exception as e:
            logger.error(f"Asaas API unexpected error: {method} {endpoint}: {str(e)}")
            raise

    def create_pix_charge(
        self,
        value: Decimal,
        description: str,
        customer_id: str,
        due_date: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        platform_wallet_id: Optional[str] = None,
        platform_fee: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """
        Creates PIX charge (cobranca) on Asaas.

        When platform_wallet_id and platform_fee are provided, adds a split
        so the platform fee is automatically routed to the BioCodeTechPay
        Asaas wallet at payment time — guaranteed by Asaas infrastructure,
        independent of webhook handler correctness.

        Split is calculated on netValue (gross minus Asaas own fee).
        The platform_fee must not exceed the expected netValue.

        Asaas API: POST /payments
        Docs: https://docs.asaas.com/reference/criar-nova-cobranca
        """
        payload = {
            "customer": customer_id,  # Asaas customer ID (created previously)
            "billingType": "PIX",
            "value": float(value),
            "description": description[:500],  # Max 500 chars
        }

        if due_date:
            payload["dueDate"] = due_date.strftime("%Y-%m-%d")
        else:
            # Default: due today (PIX charges are immediate — no deferred scheduling)
            payload["dueDate"] = datetime.now().strftime("%Y-%m-%d")

        # Automatic fee split: routes platform fee to BioCodeTechPay Asaas wallet.
        # Guarantees fee collection at the Asaas level regardless of webhook handler state.
        # The platform must NOT include its own walletId to avoid Asaas API exception.
        if platform_wallet_id and platform_fee and platform_fee > Decimal("0"):
            payload["split"] = [
                {
                    "walletId": platform_wallet_id,
                    "fixedValue": round(float(platform_fee), 2),
                }
            ]

        response = self._make_request(
            method="POST",
            endpoint="/payments",
            data=payload,
            idempotency_key=idempotency_key
        )

        # Asaas returns payment ID, now fetch PIX QR Code
        payment_id = response.get("id")
        if not payment_id:
            raise ValueError("Asaas API did not return payment ID")

        # Fetch PIX QR Code
        qr_code_response = self._make_request(
            method="GET",
            endpoint=f"/payments/{payment_id}/pixQrCode"
        )

        return {
            "charge_id": payment_id,
            "qr_code": qr_code_response.get("payload", ""),  # Copy-paste code
            "qr_code_url": qr_code_response.get("encodedImage", ""),  # Base64 image
            "status": response.get("status", "PENDING"),
            "expires_at": datetime.strptime(payload["dueDate"], "%Y-%m-%d") if payload.get("dueDate") else None
        }

    def create_pix_payment(
        self,
        value: Decimal,
        pix_key: str,
        pix_key_type: str,
        description: str,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates PIX payment (transferência) on Asaas.

        Asaas API: POST /transfers
        Docs: https://docs.asaas.com/reference/realizar-transferencia-pix

        Note: Asaas requires that the destination PIX key is registered in the platform.
        For external keys, use POST /pix/qrCodes/pay (decode QR Code payment).
        """
        # Map internal key types to Asaas format
        # Internal enum values: CPF, CNPJ, EMAIL, TELEFONE, ALEATORIA
        asaas_key_type_map = {
            "CPF": "CPF",
            "CNPJ": "CNPJ",
            "EMAIL": "EMAIL",
            "TELEFONE": "PHONE",    # PixKeyType.PHONE.value = "TELEFONE"
            "ALEATORIA": "EVP",    # PixKeyType.RANDOM.value = "ALEATORIA" → Asaas EVP
            # Legacy keys kept for backward compatibility
            "PHONE": "PHONE",
            "RANDOM": "EVP"
        }

        payload = {
            "value": float(value),
            "pixAddressKey": pix_key,
            "pixAddressKeyType": asaas_key_type_map.get(pix_key_type, "EVP"),
            "description": description[:140]  # Max 140 chars for PIX
        }

        # Resolve operationKey: TOTP code (dynamic) or static key, whichever is configured.
        # This is required to bypass Asaas device-based authorization automatically.
        op_key = self._get_operation_key()
        if op_key:
            payload["operationKey"] = op_key

        response = self._make_request(
            method="POST",
            endpoint="/transfers",
            data=payload,
            idempotency_key=idempotency_key
        )

        asaas_status = response.get("status", "")

        # AWAITING_TRANSFER_AUTHORIZATION is the expected status when the withdrawal
        # validation webhook is enabled. Asaas will call /validacao-saque, receive
        # {"approved": true} and process the transfer automatically — no SMS, no manual step.
        # Treat it as BANK_PROCESSING from the application perspective.
        normalized_status = asaas_status
        if asaas_status == "AWAITING_TRANSFER_AUTHORIZATION":
            normalized_status = "BANK_PROCESSING"
            logger.info(
                f"PIX transfer awaiting webhook validation: id={response.get('id')}. "
                "Asaas will call /validacao-saque and approve automatically."
            )

        return {
            "payment_id": response.get("id"),
            "status": normalized_status or "BANK_PROCESSING",
            "value": float(value),  # always echo the requested value so the router can debit correctly
            "end_to_end_id": response.get("endToEndIdentifier"),
            "processed_at": datetime.fromisoformat(response["dateCreated"]) if response.get("dateCreated") else None
        }

    def pay_qr_code(
        self,
        payload: str,
        description: str = "",
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Pays a PIX QR Code (EMV payload / Pix Copia e Cola).

        Asaas API: POST /pix/qrCodes/pay
        Docs: https://docs.asaas.com/reference/pagar-qr-code-pix

        Fallback: if Asaas returns parse_error on the qrCode field (which occurs for
        static QR codes from external maquininhas/PSPs), the method parses the Pix key
        and amount directly from the EMV payload and falls back to POST /transfers.
        This preserves full interoperability with all valid BR Code QR codes.

        Args:
            payload: Full EMV string (000201...)
            description: Optional payment description (max 140 chars)
            idempotency_key: Idempotency key to prevent duplicate payments
        """
        import json as _json
        from app.core.pix_emv import parse_emv_pix_key, parse_emv_amount

        body = {
            "qrCode": payload,
            "description": (description or "BioCodeTechPay QR Code Payment")[:140]
        }

        op_key = self._get_operation_key()
        if op_key:
            body["operationKey"] = op_key

        try:
            response = self._make_request(
                method="POST",
                endpoint="/pix/qrCodes/pay",
                data=body,
                idempotency_key=idempotency_key
            )
        except httpx.HTTPStatusError as exc:
            # Asaas /pix/qrCodes/pay rejects external PSP QR codes with parse_error.
            # Two fallback strategies based on QR type:
            #
            # Path 1 — Static QR (key in field 26/01):
            #   parse_emv_pix_key extracts the key → POST /transfers directly.
            #
            # Path 2 — Dynamic QR (payloadLocation URL in field 26/25 or 26/01):
            #   - Fetch the PSP URL to get the active charge data.
            #   - Extract 'chave' (Pix key) and 'valor.original' from the JSON response.
            #   - POST /transfers with the resolved key.
            #   - If the PSP returns 404/410 or status EXPIRADA → raise ValueError
            #     with a user-facing message (router maps it to HTTP 422).
            if exc.response.status_code == 400:
                try:
                    err_body = _json.loads(exc.response.text)
                    errors = err_body.get("errors", [])
                    has_parse_error = any(e.get("code") == "parse_error" for e in errors)
                except Exception:
                    has_parse_error = False

                if has_parse_error:
                    import re as _re
                    from app.core.pix_emv import (
                        parse_emv_pix_key,
                        parse_emv_amount,
                        parse_emv_payload_url,
                    )

                    pix_key, key_type = parse_emv_pix_key(payload)
                    emv_amount = parse_emv_amount(payload)

                    # --- Path 1: static QR — Pix key embedded in field 26/01 ---
                    if pix_key and emv_amount > 0:
                        logger.info(
                            f"pay_qr_code: parse_error — static QR fallback to /transfers: "
                            f"key_type={key_type} "
                            f"key={pix_key[:30]}{'...' if len(pix_key) > 30 else ''} "
                            f"value={emv_amount}"
                        )
                        # Idempotency key must be deterministic for the same static QR payload
                        # so retries reuse the same Asaas operation instead of creating a new charge.
                        import hashlib as _hashlib_s
                        _ph_static = _hashlib_s.sha256(payload.encode()).hexdigest()[:20]
                        _det_key_static = idempotency_key or f"qrstatic-{_ph_static}"
                        result = self.create_pix_payment(
                            value=Decimal(str(emv_amount)),
                            pix_key=pix_key,
                            pix_key_type=key_type,
                            description=description or "BioCodeTechPay QR Code Payment",
                            idempotency_key=_det_key_static
                        )
                        # Ensure the caller always sees the value — create_pix_payment strips it.
                        result.setdefault("value", emv_amount)
                        return result

                    # --- Path 2: dynamic QR — fetch payloadLocation, resolve key ---
                    payload_url = parse_emv_payload_url(payload)
                    if payload_url:
                        try:
                            with httpx.Client(
                                timeout=httpx.Timeout(5.0, connect=3.0),
                                follow_redirects=True
                            ) as _client:
                                _url_resp = _client.get(
                                    payload_url,
                                    headers={
                                        "Accept": "application/json, */*",
                                        "Cache-Control": "no-cache",
                                        "User-Agent": (
                                            "Mozilla/5.0 (Linux; Android 12) "
                                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                                            "Chrome/121.0.0.0 Mobile Safari/537.36"
                                        ),
                                    },
                                )

                            if _url_resp.status_code in (404, 410):
                                raise ValueError(
                                    "QR Code expirado ou removido pelo estabelecimento. "
                                    "Gere um novo QR Code no terminal e tente novamente."
                                )
                            _url_resp.raise_for_status()

                            # BACEN PIX API spec (v2.4) allows PSPs to return their
                            # payloadLocation responses as JWS (JSON Web Signature)
                            # with Content-Type: application/jose.
                            # PagSeguro and some other PSPs use this security-enhanced
                            # format: the body is a compact JWS (header.payload.sig)
                            # where the payload base64url-decodes to the charge JSON.
                            # We must handle both plain JSON and JWS-signed responses.
                            import base64 as _b64_mod
                            _resp_text = _url_resp.text.strip() if _url_resp.content else ""
                            _ct = _url_resp.headers.get("content-type", "").lower()

                            if not _resp_text:
                                raise ValueError(
                                    "QR Code dinamico nao pode ser processado: "
                                    "PSP retornou resposta vazia (payloadLocation inacessivel)."
                                )

                            if "jose" in _ct or (not _resp_text.startswith("{") and _resp_text.count(".") >= 2):
                                # JWS compact serialization: header.payload.signature
                                _jws_parts = _resp_text.split(".")
                                if len(_jws_parts) < 3:
                                    raise ValueError(
                                        "QR Code dinamico nao pode ser processado: "
                                        "formato JWS invalido retornado pelo PSP."
                                    )
                                _payload_b64 = _jws_parts[1]
                                _pad = 4 - len(_payload_b64) % 4
                                if _pad != 4:
                                    _payload_b64 += "=" * _pad
                                try:
                                    charge_data = _json.loads(
                                        _b64_mod.urlsafe_b64decode(_payload_b64)
                                    )
                                except Exception as _jws_err:
                                    raise ValueError(
                                        "QR Code dinamico nao pode ser processado: "
                                        "falha ao decodificar resposta JWS do PSP."
                                    ) from _jws_err
                                logger.info(
                                    f"pay_qr_code: JWS payloadLocation decoded successfully "
                                    f"(status={_url_resp.status_code}, url={payload_url[:80]})"
                                )
                            else:
                                charge_data = _url_resp.json()

                        except ValueError:
                            raise
                        except Exception as _url_err:
                            logger.warning(
                                f"pay_qr_code: dynamic QR payloadLocation fetch failed: "
                                f"url={payload_url[:80]} err={_url_err}."
                            )
                            raise ValueError(
                                "QR Code dinamico nao pode ser processado no momento. "
                                "Verifique se o QR Code ainda e valido e tente novamente, "
                                "ou solicite um QR Code estatico ao vendedor."
                            ) from _url_err

                        # Check PSP charge status before transferring
                        _psp_status = (charge_data.get("status") or "").upper()
                        _EXPIRED = {
                            "EXPIRADA",
                            "REMOVIDA_PELO_USUARIO_RECEBEDOR",
                            "REMOVIDA_PELO_PSP",
                            "CONCLUIDA",
                        }
                        if _psp_status in _EXPIRED:
                            raise ValueError(
                                f"QR Code expirado (status PSP: {_psp_status}). "
                                "Gere um novo QR Code no terminal e tente novamente."
                            )

                        resolved_key = (charge_data.get("chave") or "").strip()
                        if not resolved_key:
                            raise ValueError(
                                "PSP nao retornou chave Pix na resposta do QR Code dinamico. "
                                "Formato de QR Code nao suportado."
                            )

                        # Amount: prefer PSP response; fall back to EMV field 54
                        _raw_val = (
                            (charge_data.get("valor") or {}).get("original")
                            or (charge_data.get("valor") or {}).get("modalidadeAlteracao")
                        )
                        resolved_amount = (
                            float(_raw_val) if _raw_val else emv_amount
                        )
                        if resolved_amount <= 0:
                            raise ValueError(
                                "Valor do QR Code dinamico nao identificado. "
                                "Verifique o QR Code e tente novamente."
                            )

                        # Classify key type from resolved key value
                        if "@" in resolved_key:
                            resolved_key_type = "EMAIL"
                        elif _re.match(r'^\d{14}$', resolved_key):
                            resolved_key_type = "CNPJ"
                        elif _re.match(r'^\d{11}$', resolved_key):
                            resolved_key_type = "CPF"
                        elif resolved_key.startswith("+"):
                            resolved_key_type = "PHONE"
                        else:
                            resolved_key_type = "EVP"

                        logger.info(
                            f"pay_qr_code: parse_error — dynamic QR fallback to /transfers: "
                            f"url={payload_url[:60]} "
                            f"key_type={resolved_key_type} "
                            f"key={resolved_key[:30]}{'...' if len(resolved_key) > 30 else ''} "
                            f"value={resolved_amount}"
                        )
                        # Idempotency key must be deterministic for the same QR payload.
                        # Using timestamp-based keys (frontend default) causes duplicate charges
                        # when the user retries after a transient error: Asaas treats each
                        # new key as a fresh transfer even if the previous one went through.
                        import hashlib as _hashlib
                        _payload_hash = _hashlib.sha256(payload.encode()).hexdigest()[:20]
                        _deterministic_key = idempotency_key or f"qrfallback-{_payload_hash}"
                        result = self.create_pix_payment(
                            value=Decimal(str(resolved_amount)),
                            pix_key=resolved_key,
                            pix_key_type=resolved_key_type,
                            description=description or "BioCodeTechPay QR Code Payment",
                            idempotency_key=_deterministic_key,
                        )
                        # Ensure the caller always sees the resolved value.
                        result.setdefault("value", resolved_amount)
                        return result
            raise

        asaas_status = response.get("status", "BANK_PROCESSING")
        if asaas_status == "AWAITING_TRANSFER_AUTHORIZATION":
            asaas_status = "BANK_PROCESSING"
            logger.info(
                f"QR Code payment awaiting webhook validation: id={response.get('id')}. "
                "Asaas will call /validacao-saque and approve automatically."
            )

        pix_tx = response.get("pixTransaction") or {}

        # value may be absent at top level when status is AWAITING_TRANSFER_AUTHORIZATION;
        # fall back to pixTransaction.value which Asaas always populates.
        resolved_value = response.get("value") or pix_tx.get("value")

        return {
            "payment_id": response.get("id"),
            "status": asaas_status,
            "value": resolved_value,
            "end_to_end_id": response.get("endToEndIdentifier"),
            "receiver_name": pix_tx.get("receiverName") or "",
            "processed_at": datetime.fromisoformat(response["dateCreated"]) if response.get("dateCreated") else None
        }

    def get_charge_status(self, charge_id: str) -> Dict[str, Any]:
        """
        Retrieves PIX charge status from Asaas.

        Asaas API: GET /payments/{id}
        """
        response = self._make_request(
            method="GET",
            endpoint=f"/payments/{charge_id}"
        )

        # Map Asaas status to internal status
        status_map = {
            "PENDING": "PENDING",
            "RECEIVED": "CONFIRMED",
            "CONFIRMED": "CONFIRMED",
            "OVERDUE": "EXPIRED",
            "REFUNDED": "CANCELLED"
        }

        return {
            "charge_id": response.get("id"),
            "status": status_map.get(response.get("status"), "PENDING"),
            "paid_at": datetime.fromisoformat(response["paymentDate"]) if response.get("paymentDate") else None,
            "payer_info": {
                "name": response.get("customerName"),
                "document": response.get("customerDocument")
            } if response.get("customerName") else None
        }

    def get_payment_status(self, payment_id: str) -> Dict[str, Any]:
        """
        Retrieves PIX payment status from Asaas.

        Asaas API: GET /transfers/{id}
        """
        response = self._make_request(
            method="GET",
            endpoint=f"/transfers/{payment_id}"
        )

        status_map = {
            "PENDING": "PENDING",
            "BANK_PROCESSING": "PROCESSING",
            "DONE": "CONFIRMED",
            "CANCELLED": "FAILED",
            "FAILED": "FAILED"
        }

        return {
            "payment_id": response.get("id"),
            "status": status_map.get(response.get("status"), "PENDING"),
            "end_to_end_id": response.get("endToEndIdentifier"),
            "failure_reason": response.get("failReason") if response.get("status") == "FAILED" else None,
            "receiver_name": response.get("receiverName") or response.get("receiver_name")
        }

    def cancel_charge(self, charge_id: str) -> bool:
        """
        Cancels a pending PIX charge on Asaas.

        Asaas API: DELETE /payments/{id}
        """
        try:
            self._make_request(
                method="DELETE",
                endpoint=f"/payments/{charge_id}"
            )
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Charge {charge_id} not found for cancellation")
                return False
            raise

    def create_customer(self, name: str, cpf_cnpj: str, email: str) -> str:
        """
        Creates or retrieves customer on Asaas.
        Required before creating charges.

        Asaas API: POST /customers

        Returns:
            Asaas customer ID
        """
        payload = {
            "name": name,
            "cpfCnpj": cpf_cnpj.replace(".", "").replace("-", "").replace("/", ""),
            "email": email
        }

        response = self._make_request(
            method="POST",
            endpoint="/customers",
            data=payload,
            idempotency_key=cpf_cnpj  # Use CPF/CNPJ as idempotency to avoid duplicates
        )

        return response.get("id")

    def create_subconta(
        self,
        name: str,
        email: str,
        cpf_cnpj: str,
        mobile_phone: str,
        address: str,
        address_number: str,
        postal_code: str,
        city: str,
        state: str,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """
        Creates an Asaas subconta (sub-account) under the parent account.

        Asaas API: POST /v3/accounts
        Docs: https://docs.asaas.com/reference/criar-subconta

        Each registered user gets their own walletId to support segregated
        wallet routing under the parent platform account.

        Args:
            name: Full legal name of the account holder
            email: Account email (unique in Asaas)
            cpf_cnpj: CPF (11 digits) or CNPJ (14 digits) — digits only
            mobile_phone: Mobile phone with DDD — digits only (e.g. 11999998888)
            address: Street name
            address_number: Street number
            postal_code: CEP — digits only (8 digits)
            city: City name
            state: 2-letter state abbreviation (e.g. SP)
            idempotency_key: Optional idempotency key; defaults to cpf_cnpj

        Returns:
            walletId (UUID string) of the created subconta

        Raises:
            ValueError: When Asaas does not return a walletId
            httpx.HTTPStatusError: On API rejection (4xx/5xx)
        """
        digits_only = re.compile(r'\D')
        payload: Dict[str, Any] = {
            "name": name,
            "email": email,
            "cpfCnpj": digits_only.sub("", cpf_cnpj),
            "mobilePhone": digits_only.sub("", mobile_phone),
            "address": address,
            "addressNumber": address_number,
            "province": city,   # neighborhood (bairro) — use city as fallback
            "postalCode": digits_only.sub("", postal_code),
            "city": city,
            "state": state.upper(),
        }

        response = self._make_request(
            method="POST",
            endpoint="/accounts",
            data=payload,
            idempotency_key=idempotency_key or digits_only.sub("", cpf_cnpj),
        )

        wallet_id: Optional[str] = response.get("walletId")
        if not wallet_id:
            raise ValueError(
                f"Asaas subconta creation did not return walletId. "
                f"Response keys: {list(response.keys())}"
            )

        logger.info(
            f"Asaas subconta created: doc={digits_only.sub('', cpf_cnpj)[-4:]}*** "
            f"walletId={wallet_id}"
        )
        return wallet_id

    def decode_qr_code(self, payload: str) -> Optional[Dict[str, Any]]:
        """
        Decodes a PIX QR Code EMV payload via Asaas POST /pix/qrCodes/decode.

        Raises:
            httpx.HTTPStatusError: When Asaas rejects the QR Code (4xx) — expired or invalid.
                The caller MUST catch this to translate the error to a user-facing message.
        Returns:
            dict with value and beneficiary_name on success.
            None when the gateway is unreachable (network/timeout errors only).
        """
        try:
            response = self._make_request(
                method="POST",
                endpoint="/pix/qrCodes/decode",
                data={"payload": payload}
            )
        except httpx.HTTPStatusError:
            # Asaas API error (expired/invalid QR) — propagate so caller can handle
            raise
        except Exception as e:
            logger.warning(f"QR Code decode: network/timeout error: {e}")
            return None

        if not response:
            return None
        return {
            "value": response.get("value"),
            "beneficiary_name": (
                response.get("receiverName")
                or response.get("name")
                or "Beneficiario"
            ),
        }

    def lookup_pix_key(self, pix_key: str, key_type: str) -> Optional[Dict[str, Any]]:
        """
        Looks up a PIX key via Asaas DICT.

        Return contract:
          - dict with name/document/bank  → key confirmed in DICT
          - {"found": False, "reason": "not_in_dict"}  → Asaas 404: key not registered
          - {"found": False, "reason": "invalid_format"} → Asaas 400/422: bad key format
          - None  → gateway unavailable (network error, 5xx, timeout, sandbox)
                    caller must allow the transfer to proceed (soft pass)
        """
        import httpx as _httpx
        asaas_key_type_map = {
            "CPF": "CPF",
            "CNPJ": "CNPJ",
            "EMAIL": "EMAIL",
            "TELEFONE": "PHONE",
            "ALEATORIA": "EVP",
            "PHONE": "PHONE",
            "RANDOM": "EVP",
        }
        try:
            response = self._make_request(
                method="GET",
                endpoint="/pix/addressKeys/info",
                params={
                    "value": pix_key,
                    "type": asaas_key_type_map.get(key_type, "EVP")
                }
            )
            if not response:
                return None
            account = response.get("account") or {}
            owner = response.get("owner") or {}
            return {
                "found": True,
                "name": owner.get("name") or response.get("name"),
                "document": mask_sensitive_data(
                    owner.get("taxId") or response.get("cpfCnpj") or pix_key,
                    visible_chars=4
                ),
                "bank": account.get("name") or response.get("ispbName") or "Instituicao bancaria",
                "key_type": key_type,
                "pix_key": pix_key,
            }
        except _httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 404:
                # Asaas confirmed: chave nao registrada no DICT
                logger.info(f"PIX DICT lookup: key not registered (404) key={mask_sensitive_data(pix_key)}")
                return {"found": False, "reason": "not_in_dict"}
            if status in (400, 422):
                # Formato invalido para o tipo informado
                logger.info(f"PIX DICT lookup: invalid key format ({status}) key={mask_sensitive_data(pix_key)}")
                return {"found": False, "reason": "invalid_format"}
            # 5xx ou outro erro HTTP → gateway indisponivel, soft pass
            logger.warning(f"PIX key lookup gateway error ({status}) key={mask_sensitive_data(pix_key)}: {e}")
            return None
        except Exception as e:
            # Timeout, network, sandbox sem suporte — soft pass, nao bloquear envio
            logger.warning(f"PIX key lookup unavailable key={mask_sensitive_data(pix_key)}: {e}")
            return None
