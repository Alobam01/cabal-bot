from sqlalchemy import Column, BigInteger, String, Boolean, JSON, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class UserConfig(Base):
    __tablename__ = "user_configs"
    
    telegram_id = Column(BigInteger, primary_key=True)
    session_string = Column(Text, nullable=True)
    source_groups = Column(JSON, default=list)      # max 3
    target_group = Column(String, nullable=True)
    training_examples = Column(JSON, default=list)
    seen_cas = Column(JSON, default=list)
    
    subscription_status = Column(String, default="none")  # none, trial, active, expired
    trial_start = Column(DateTime, nullable=True)
    subscription_expiry = Column(DateTime, nullable=True)
    last_payment_tx = Column(String, nullable=True)
    
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
