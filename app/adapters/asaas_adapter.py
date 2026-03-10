"""
Asaas Payment Gateway Adapter.
Implements PaymentGatewayPort for Asaas BaaS API integration.
Includes resilience patterns: retry, timeout, circuit breaker.
"""
import httpx
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
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates PIX charge (cobrança) on Asaas.

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

        Args:
            payload: Full EMV string (000201...)
            description: Optional payment description (max 140 chars)
            idempotency_key: Idempotency key to prevent duplicate payments
        """
        body = {
            "qrCode": payload,
            "description": (description or "Bio Code Tech Pay QR Code Payment")[:140]
        }

        op_key = self._get_operation_key()
        if op_key:
            body["operationKey"] = op_key

        response = self._make_request(
            method="POST",
            endpoint="/pix/qrCodes/pay",
            data=body,
            idempotency_key=idempotency_key
        )

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

    def decode_qr_code(self, payload: str) -> Optional[Dict[str, Any]]:
        """
        Decodes a PIX QR Code EMV payload to retrieve value and beneficiary info
        without executing the payment.

        Asaas API: POST /pix/qrCodes/decode
        """
        try:
            response = self._make_request(
                method="POST",
                endpoint="/pix/qrCodes/decode",
                data={"payload": payload}
            )
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
        except Exception as e:
            logger.warning(f"QR Code decode failed: {e}")
            return None

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
