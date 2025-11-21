from datetime import datetime, timedelta, timezone
import re

def mask_cpf_cnpj(doc: str) -> str:
    """
    Masks CPF or CNPJ.
    CPF: ***.123.456-**
    CNPJ: **.***.123/0001-**
    """
    clean_doc = re.sub(r'\D', '', doc)

    if len(clean_doc) == 11: # CPF
        return f"***.{clean_doc[3:6]}.{clean_doc[6:9]}-**"
    elif len(clean_doc) == 14: # CNPJ
        return f"**.***.{clean_doc[5:8]}/{clean_doc[8:12]}-**"
    else:
        # Fallback for other keys (email, phone) or invalid docs
        if "@" in doc: # Email
            user, domain = doc.split("@")
            return f"{user[:2]}***@{domain}"
        return f"{doc[:3]}***{doc[-2:]}"

def format_brasilia_time(dt: datetime) -> str:
    """
    Converts UTC datetime to Brasília time (UTC-3) and formats it.
    Format: DD/MM/YYYY at HH:mm:ss
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Brasilia is UTC-3 (ignoring DST as it's abolished)
    brasilia_tz = timezone(timedelta(hours=-3))
    dt_brasilia = dt.astimezone(brasilia_tz)

    return dt_brasilia.strftime("%d/%m/%Y at %H:%M:%S")
