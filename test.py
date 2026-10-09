
import random
import secrets
import uuid

# 1. random: general-purpose randomness
print("Random number:", random.randint(100000, 999999))

# 2. secrets: security-sensitive randomness
print("Secure OTP:", f"{secrets.randbelow(1_000_000):06d}")
print("Secure session ID:", secrets.token_urlsafe(32))

# 3. uuid: unique identifier
print("UUID:", uuid.uuid4())
