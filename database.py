from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import urllib.parse

# -------------------------------
# ✅ Use one of these DB URLs
# -------------------------------

# Example PostgreSQL URL
# NOTE: Encode special characters in password, e.g., @ → %40
DATABASE_URL = "postgresql://postgres:Admin%40123@localhost:5432/transaction_apis"

# Example MSSQL URL with ODBC Driver 17 (Uncomment to use MSSQL)
# DATABASE_URL = (
#     "mssql+pyodbc://sa:Sameera%4018@localhost:1433/transaction?driver=ODBC+Driver+17+for+SQL+Server"
# )

# Create SQLAlchemy engine
engine = create_engine(DATABASE_URL)

# Session configuration
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()
