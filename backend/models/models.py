from sqlalchemy import String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass
class IncomingEmail(Base):
    __tablename__='emails'
    id: Mapped[str]=mapped_column(String,primary_key=True)
    sender: Mapped[str]=mapped_column(String)
    subject: Mapped[str]=mapped_column(String)
    body: Mapped[str]=mapped_column(Text)
    received_at: Mapped[str]=mapped_column(String)
    fingerprint: Mapped[str]=mapped_column(String,unique=True)
class PropertyDeal(Base):
    __tablename__='deals'
    id: Mapped[str]=mapped_column(String,primary_key=True)
    email_id: Mapped[str]=mapped_column(ForeignKey('emails.id'))
    property: Mapped[str]=mapped_column(Text)
    evaluation: Mapped[str]=mapped_column(Text)
    draft: Mapped[str]=mapped_column(Text)
    extraction: Mapped[str]=mapped_column(String)
    created_at: Mapped[str]=mapped_column(String)
class Approval(Base):
    __tablename__='approvals'
    id: Mapped[str]=mapped_column(String,primary_key=True)
    deal_id: Mapped[str]=mapped_column(ForeignKey('deals.id'))
    payload: Mapped[str]=mapped_column(Text)
    payload_hash: Mapped[str]=mapped_column(String)
    status: Mapped[str]=mapped_column(String)
    created_at: Mapped[str]=mapped_column(String)
    deadline: Mapped[str]=mapped_column(String)
    decided_at: Mapped[str|None]=mapped_column(String)
    sender: Mapped[str|None]=mapped_column(String)
    comments: Mapped[str|None]=mapped_column(Text)
    notification: Mapped[str]=mapped_column(Text)
class EmailExecution(Base):
    __tablename__='executions'
    id: Mapped[str]=mapped_column(String,primary_key=True)
    approval_id: Mapped[str]=mapped_column(ForeignKey('approvals.id'),unique=True)
    status: Mapped[str]=mapped_column(String)
    payload: Mapped[str]=mapped_column(Text)
    executed_at: Mapped[str]=mapped_column(String)
class MailboxMessage(Base):
    __tablename__='mailbox_messages'
    id: Mapped[str]=mapped_column(String,primary_key=True)
    received_at: Mapped[str]=mapped_column(String)
    result: Mapped[str]=mapped_column(String)
