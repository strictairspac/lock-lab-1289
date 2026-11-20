from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, Text, UniqueConstraint, Boolean, text
from datetime import datetime, timezone
from src.config.database import Base

class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False)
    password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)

class Sample(Base):
    __tablename__='samples'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    title=Column(String(160),nullable=False);quantity=Column(Integer,nullable=False);location=Column(String(120),nullable=False)
    # A child sample is a physical aliquot of its parent.  Once a parent has
    # been split it stops accepting new transfer requests (accepting=false).
    parent_id=Column(Integer,ForeignKey('samples.id'),nullable=True)
    allocation_batch_id=Column(Integer,nullable=True)
    accepting=Column(Boolean,nullable=False,default=True,server_default=text('true'))
    # The transfer application this child portion is earmarked for.
    transfer_id=Column(Integer,nullable=True)

class Transfer(Base):
    __tablename__='transfers'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False);destination=Column(String(120),nullable=False)
    quantity=Column(Integer,nullable=False);note=Column(String(300),default='')
    # pending: waiting for an aliquot allocation; allotted: fulfilled and
    # re-hooked to the child samples produced by the split.
    status=Column(String(20),nullable=False,default='pending',server_default='pending')
    allocation_batch_id=Column(Integer,nullable=True)
    source_sample_id=Column(Integer,ForeignKey('samples.id'),nullable=True)

class Trial(Base):
    __tablename__='trials'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),nullable=False);timeout_ms=Column(Integer,nullable=False)
    status=Column(String(20),default='queued',nullable=False);result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));error=Column(Text,default='')
class TrialBalance(Base):
    __tablename__='trial_balances'
    trial_id=Column(Integer,ForeignKey('trials.id'),primary_key=True);remaining=Column(Integer,nullable=False);received=Column(Integer,nullable=False,default=0)
class Receipt(Base):
    __tablename__='receipts'
    id=Column(Integer,primary_key=True);trial_id=Column(Integer,ForeignKey('trials.id'),nullable=False)
    actor=Column(String(20),nullable=False);quantity=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('trial_id','actor'),)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))

class AllocationBatch(Base):
    """A frozen allocation preview; confirming it is an idempotent one-shot."""
    __tablename__='allocation_batches'
    id=Column(Integer,primary_key=True)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False)
    status=Column(String(20),nullable=False,default='preview')
    expected_quantity=Column(Integer,nullable=False)
    portions=Column(JSON,nullable=False)
    assignments=Column(JSON,nullable=False)
    optimal=Column(Boolean,nullable=False,default=True,server_default=text('true'))
    preview_transfers=Column(JSON,nullable=False)
    request_key=Column(String(80),nullable=True,unique=True)
    result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    confirmed_at=Column(DateTime(timezone=True),nullable=True)
class TransferAllocation(Base):
    """Re-hook of one transfer application onto the child samples it received."""
    __tablename__='transfer_allocations'
    id=Column(Integer,primary_key=True)
    batch_id=Column(Integer,ForeignKey('allocation_batches.id'),nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),nullable=False)
    child_sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False)
    portion_code=Column(String(80),nullable=False)
    quantity=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('child_sample_id'),UniqueConstraint('transfer_id','child_sample_id'))
