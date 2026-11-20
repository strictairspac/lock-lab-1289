from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL
class Base(DeclarativeBase): pass
_options={'pool_pre_ping':True}
if DATABASE_URL.startswith('sqlite'):
    # Tests use a file-backed SQLite database with several connections.
    _options['connect_args']={'check_same_thread':False,'timeout':30}
engine = create_engine(DATABASE_URL, **_options)
Session = sessionmaker(engine, expire_on_commit=False)
def db_session():
    with Session() as db: yield db
