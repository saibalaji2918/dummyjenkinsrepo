from sqlalchemy import Column, Integer, String
from .database import Base

class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True,autoincrement=True, index=True)
    emp_code = Column(String)
    punch_datetime = Column(String)
    area_name = Column(String)
    terminal_sn = Column(String)