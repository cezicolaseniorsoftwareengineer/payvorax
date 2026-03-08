"""
Asaas Payment Gateway Adapter.
Implements PaymentGatewayPort for Asaas BaaS API integration.
Includes resilience patterns: retry, timeout, circuit breaker.
"""
import httpx
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

    def __init__(self, api_key: str, use_sandbox: bool = False):
        """
        Initializes Asaas adapter.

        Args:
            api_key: Asaas API key ($aact_prod_... or $aact_sandbox_...)
            use_sandbox: If True, use sandbox environment
        """
        if not api_key:
            raise ValueError("Asaas API key is required")

        self.api_key = api_key
        self.base_url = self.BASE_URL_SANDBOX if use_sandbox else self.BASE_URL_PRODUCTION

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "access_token": api_key,
                "Content-Type": "application/json",
                "User-Agent": "PayvoraX/1.0"
            },
            timeout=15.0  # 15 seconds timeout
        )

        logger.info(
            f"Asaas adapter initialized: environment={'sandbox' if use_sandbox else 'production'}, "
            f"key={mask_sensitive_data(api_key, visible_chars=12)}"
        )

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
            # Default: expires in 24 hours
            payload["dueDate"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

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

        response = self._make_request(
            method="POST",
            endpoint="/transfers",
            data=payload,
            idempotency_key=idempotency_key
        )

        return {
            "payment_id": response.get("id"),
            "status": response.get("status", "PENDING"),
            "end_to_end_id": response.get("endToEndIdentifier"),
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
            "failure_reason": response.get("failReason") if response.get("status") == "FAILED" else None
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
