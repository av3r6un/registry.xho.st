from datetime import UTC, datetime


def utcnow():
  """Naive UTC for the existing SQL DateTime schema."""
  return datetime.now(UTC).replace(tzinfo=None)
