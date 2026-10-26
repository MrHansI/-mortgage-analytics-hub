import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mortgage.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
CBR_API_BASE = os.getenv("CBR_API_BASE", "https://www.cbr.ru/dataservice")
CBR_TIMEOUT = float(os.getenv("CBR_TIMEOUT", "25"))
