"""
Document validation for CPF and CNPJ.
Implements the official Brazilian government validation algorithms.
Used as anti-fraud and KYC control at account registration.
"""
import re


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def validate_cpf(cpf: str) -> bool:
    """
    Validates CPF using the official Receita Federal algorithm.
    Returns False for invalid CPF, sequences of repeated digits,
    and CPFs with wrong check digits.
    """
    cpf = _digits_only(cpf)

    if len(cpf) != 11:
        return False

    # Reject trivially invalid sequences (e.g. 00000000000, 11111111111)
    if len(set(cpf)) == 1:
        return False

    # First check digit
    total = sum(int(cpf[i]) * (10 - i) for i in range(9))
    remainder = (total * 10) % 11
    first_digit = 0 if remainder >= 10 else remainder
    if first_digit != int(cpf[9]):
        return False

    # Second check digit
    total = sum(int(cpf[i]) * (11 - i) for i in range(10))
    remainder = (total * 10) % 11
    second_digit = 0 if remainder >= 10 else remainder
    if second_digit != int(cpf[10]):
        return False

    return True


def validate_cnpj(cnpj: str) -> bool:
    """
    Validates CNPJ using the official Receita Federal algorithm.
    Returns False for invalid CNPJ, sequences of repeated digits,
    and CNPJs with wrong check digits.
    """
    cnpj = _digits_only(cnpj)

    if len(cnpj) != 14:
        return False

    # Reject trivially invalid sequences (e.g. 00000000000000)
    if len(set(cnpj)) == 1:
        return False

    # First check digit
    weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    total = sum(int(cnpj[i]) * weights1[i] for i in range(12))
    remainder = total % 11
    first_digit = 0 if remainder < 2 else 11 - remainder
    if first_digit != int(cnpj[12]):
        return False

    # Second check digit
    weights2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    total = sum(int(cnpj[i]) * weights2[i] for i in range(13))
    remainder = total % 11
    second_digit = 0 if remainder < 2 else 11 - remainder
    if second_digit != int(cnpj[13]):
        return False

    return True


def validate_document(cpf_cnpj: str) -> tuple[bool, str]:
    """
    Validates a CPF or CNPJ string.

    Returns:
        (True, "CPF") if valid CPF
        (True, "CNPJ") if valid CNPJ
        (False, reason) if invalid
    """
    digits = _digits_only(cpf_cnpj)

    if len(digits) == 11:
        if validate_cpf(digits):
            return True, "CPF"
        return False, "CPF invalido. Verifique os digitos e tente novamente."

    if len(digits) == 14:
        if validate_cnpj(digits):
            return True, "CNPJ"
        return False, "CNPJ invalido. Verifique os digitos e tente novamente."

    return False, "Documento deve ter 11 digitos (CPF) ou 14 digitos (CNPJ)."
