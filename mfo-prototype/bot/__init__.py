# bot - the Telegram bot of the MFO (aiogram 3, long polling). It is a separate program:
# it shares only the database layer (app/db/database.py) and the settings (app/core/config.py)
# with the web app, never the web code.
#
#   main.py          handlers, polling, entry point        python -m bot
#   service.py       pairing codes, links, ownership-checked reads
#   staff.py         staff allowlist (numeric Telegram ids) and client search
#   texts.py         Russian texts, HTML escaping
#   operator_cli.py  local operator commands                 python -m bot.operator_cli issue --client-id 1
