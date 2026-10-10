# Wild Animal Facts - Telegram/Facebook hourly bot

Same system as Pocket Change History, but the topic is animals.
Every run: pick an animal from Wikipedia -> AI writes a source-grounded post -> fact checker -> wildlife image
(Pollinations) with headline overlay -> sent to Telegram (and optionally a Facebook Page).

1. Create a PRIVATE GitHub repo and upload everything in this folder (keep fonts/Anton-Regular.ttf).
2. Telegram: create a bot with @BotFather, add it as ADMIN of your channel, get the channel id (-100xxxxxxxxxx).
3. Pollinations key (sk_...) from https://enter.pollinations.ai/keys
4. OpenRouter key(s) for the text models (one per line or comma separated).
5. Repo > Settings > Secrets and variables > Actions:
   Secrets: POLLINATIONS_API_KEY, OPENROUTER_API_KEYS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   (optional) FB_PAGE_ID, FB_PAGE_TOKEN
   Variables (optional): IMAGE_MODEL, WRITER_MODEL, CHECKER_MODEL, PAGE_NAME
6. Trigger the workflow from cron-job.org (workflow_dispatch API) or press "Run workflow" once to test.

Local test:  set the env vars, then  python bot.py

What changed from the original:
- CURATED_TOPICS = 220 animal Wikipedia articles (mammals, birds, reptiles, fish, insects ...)
- Writer/checker prompts: zoologist voice, units (km/h, kg, cm ...) are verified against the source
- Image style: natural-colour wildlife photo, no vintage filter, animal-anatomy QA
- No "30 years old" rule, no money rules
- history.json starts empty; each entry stores topic, source, habitat
