#!/usr/bin/env python3
"""Run post.py end-to-end locally for a real test.

Loads secrets from Chris's local .env plus the Gmail app password from the
himalaya config (values are never printed). Sends a real Telegram DM + email.
"""
import importlib.util
import os
import re

env_path = os.path.expanduser("~/.hermes/.env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))

os.environ.setdefault("TELEGRAM_CHAT_ID", "1841368859")

# Gmail app password from the himalaya config.
cfg = os.path.expanduser("~/.config/himalaya/config.toml")
if os.path.exists(cfg):
    txt = open(cfg).read()
    m = re.search(r'smtp\.sasl\.plain\.password\.raw\s*=\s*"([^"]+)"', txt)
    if m:
        os.environ.setdefault("EMAIL_ADDRESS", "chrisfarrell2012@gmail.com")
        os.environ["EMAIL_PASSWORD"] = m.group(1)
        os.environ.setdefault("EMAIL_TO", "chrisfarrell2012@gmail.com")

os.environ["ALLOW_ANY_HOUR"] = "1"

spec = importlib.util.spec_from_file_location(
    "post", os.path.join(os.path.dirname(os.path.abspath(__file__)), "post.py"))
post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post)
post.main()
print("DONE")
