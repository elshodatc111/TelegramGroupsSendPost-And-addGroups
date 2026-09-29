"""Umumiy xizmat obyektlari (bitta nusxada)."""
from .accounts import AccountManager
from .joiner import Joiner
from .sender import Sender

manager = AccountManager()
sender = Sender(manager)
joiner = Joiner(manager)
