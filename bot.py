#!/usr/bin/env python3
"""Wildlore Facts - post bot. Text via a pool of OpenRouter keys (round-robin), images via Cloudflare Workers AI (free daily allowance), Pollinations as backup."""
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

API = "https://gen.pollinations.ai"
KEY = os.environ.get("POLLINATIONS_API_KEY", "")
# Cloudflare Workers AI (free: 10,000 neurons per day, resets 00:00 UTC). Used first when both values are set.
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_MODEL = os.environ.get("CF_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip() or "@cf/black-forest-labs/flux-1-schnell"
try:
    CF_STEPS = min(8, max(1, int(os.environ.get("CF_STEPS", "4") or 4)))
except ValueError:
    CF_STEPS = 4
CF_SIZE = os.environ.get("CF_SIZE", "").strip()   # optional, e.g. "1024x1280"; empty = the model default (square)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
def _models(name, default):
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


# comma-separated lists: the first model is tried first, the next ones are fallbacks
WRITER_MODELS = _models("WRITER_MODEL", "")    # empty = automatic: best free models
CHECKER_MODELS = _models("CHECKER_MODEL", "")  # empty = automatic: a different free model family
IMAGE_MODELS = _models("IMAGE_MODEL", "lykon/dreamshaper-8-lcm,flux")  # best first; the next ones are used if it fails
PAGE_NAME = os.environ.get("PAGE_NAME", "Wildlore Facts")
SEND_NOTES = os.environ.get("SEND_NOTES", "0") == "1"
WATERMARK = os.environ.get("WATERMARK", "AI-generated illustration")

# Facebook Page posting (optional: runs only when both values are set)
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "").strip()
FB_VERSION = os.environ.get("FB_GRAPH_VERSION", "v26.0").strip()
FB_DISCLOSURE = os.environ.get("FB_DISCLOSURE", "")
try:
    FB_EVERY_HOURS = max(1, int(os.environ.get("FB_EVERY_HOURS", "1") or 1))
except ValueError:
    FB_EVERY_HOURS = 1

HERE = Path(__file__).parent
HISTORY = HERE / "history.json"
FONT_PATH = HERE / "fonts" / "Anton-Regular.ttf"
FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 1080, 1350          # final post size (4:5), photo fills the whole frame
GEN_W, GEN_H = 1024, 1280    # generate 4:5, then resize to the final size


WHITE, RED, GOLD = (255, 255, 255), (226, 38, 38), (240, 190, 90)

CATEGORIES = [
    "record-breaking animals (fastest, largest, longest-lived, deepest diver, strongest)",
    "strange body parts and senses (how an animal sees, hears, smells or feels)",
    "clever hunting, defence and camouflage tricks",
    "animal migrations and journeys",
    "surprising animal families, babies and parenting",
    "animal intelligence, tools and communication",
    "extreme survival (cold, heat, deep sea, desert, no food)",
    "tiny animals with a huge effect (insects, plankton, parasites that matter to nature)",
    "animals that look impossible but are real",
    "animals that nearly vanished and came back",
]
BANNED = (
    "dogs are colourblind, goldfish have a 3-second memory, bats are blind, lemmings jump off cliffs, "
    "ostriches bury their heads, bulls hate red, humans use 10 percent of the brain"
)

CATEGORY_KEYWORDS = [
    ["fastest animal", "largest animal", "longest-lived animal", "deepest diving mammal", "strongest animal for its size"],
    ["animal senses", "echolocation", "electroreception", "magnetoreception animals", "eyes of animals"],
    ["camouflage in animals", "venom evolution", "animal defence mechanism", "ambush predator", "mimicry animal"],
    ["animal migration", "longest migration", "bird migration", "salmon run", "monarch butterfly migration"],
    ["parental care animals", "marsupial", "egg-laying mammal", "cooperative breeding", "animal nest"],
    ["tool use in animals", "animal cognition", "animal communication", "crow intelligence", "dolphin communication"],
    ["extremophile animal", "hibernation", "desert animal adaptation", "deep sea animal", "polar animal adaptation"],
    ["keystone species", "pollinator", "coral polyp", "ant colony", "termite mound"],
    ["unusual animal", "living fossil", "deep-sea fish", "flightless bird", "venomous mammal"],
    ["species recovery", "conservation success", "reintroduction of species", "rediscovered species"],
]

WRITER_SYSTEM = """You are an expert zoologist, fact checker and viral Facebook animal-facts copywriter.
You create ORIGINAL posts about the lives of animals: how they look, hunt, sense, move, survive, raise their young and live together.
- Follow the source text closely and do not rephrase factual claims in ways that could be challenged.
- Prefer exact wording from the source for names, numbers, units and places.
- Do not invent causal links, timing, or comparisons that are not stated in the source.

HOOK RULE (most important after accuracy):
- Pick the most surprising concrete fact the SOURCE TEXT states: a number, ratio, speed, size, age, distance, ability or odd behaviour, ideally with a contrast (tiny body vs huge feat, a strange habit vs normal life).
- Avoid vague trends ("animals adapt", "nature is amazing"); prefer one specific, checkable fact and build the story around it.
- The reader must think "wait, really?" within one second.

SOURCE-GROUNDING RULES (absolute):
- You receive SOURCE TEXT from Wikipedia. Use ONLY facts that are stated in the SOURCE TEXT. Never add a number, name, cause or "first/only/largest/fastest" claim that the SOURCE TEXT does not state. Use no outside knowledge for facts.
- Write every number and unit EXACTLY as it appears in the SOURCE TEXT (if the source says "20 centimetres", write "20 centimetres"). Do no arithmetic and no conversions: never restate a measurement in another unit (not "24 in" as "two feet") and never add "nearly", "almost", "roughly" or "about" to a number unless the SOURCE TEXT uses that word.
- Every proper name (species, place, scientist) in your caption must appear in the SOURCE TEXT.
- Write every number as DIGITS (25.2 metres, 2017, 4.5 tonnes). Never spell numbers out in words (not "twenty-five point two"); only "one" and "two" may be words in ordinary phrases.
- Pick the single most surprising fact with a number that the SOURCE TEXT clearly states and that is about the animal's body, senses, behaviour, life cycle, habitat or survival. Return {"skip": true, "reason": "..."} only if the article contains no such fact at all.
- "evidence": 3 to 6 objects {"claim": "...", "quote": "..."} where quote is copied WORD FOR WORD from the SOURCE TEXT (6 to 30 words). Together they must cover EVERY number and name used in the headline and in the REVEAL paragraph.
- Never put citation markers like [1] or URLs in any text field.

FACT TYPE RULE (very important):
- Prefer concrete documented facts (a measured speed, size, weight, lifespan, distance, number of offspring, a record, a strange ability and how it works). 
- Do not state vague claims such as "typical", "average" or "many animals" unless the SOURCE TEXT states them.
- Many animal facts are disputed or vary by individual. Use cautious wording ("up to", "about", "can reach", "reportedly") exactly where the source does.
- STYLE: tell what the animal does using the source's own facts. Do not add evaluations or claims of importance that the source does not state (for example "the deadliest", "the smartest"). Rhetorical questions and reflections belong only in the CLOSER paragraph.
- Never claim an animal "feels", "wants" or "plans" something unless the source says so; describe behaviour instead.

IMAGE RULES:
- The picture must show the ANIMAL and the behaviour or trait from the story, like a real wildlife photograph: correct anatomy for that species (right number of legs, wings, eyes), natural habitat, natural light.
- Give TWO different visual ideas for the same story: image_prompt_a is the main picture, image_prompt_b is another way to show the same story (for example a close-up vs a wide habitat shot). Each is ONE vivid visual description of 40-70 words: the animal and what it is doing FIRST, then the place and habitat, the light, the mood and the camera view (close-up, telephoto, wide, underwater).
- Keep the main subject in the upper two thirds of the frame. No people, no text, no logos, no cages.
- Show only ONE or TWO animals clearly. Avoid crowds and tangled groups.
- Describe the species' REAL colours and body shape (for example deep-sea fish are dark brown or black with small eyes, a polar bear is creamy white). Name the species and its most recognisable feature (for example the very long thin lure of a whipnose angler). Never ask for bright rainbow colours, cute or cartoon faces, glowing outlines or fantasy looks.

HEADLINE SOURCE RULE (accuracy):
- Build the headline and the main claim ONLY from a sentence of the SOURCE TEXT whose meaning is completely clear. If a sentence is ambiguous (for example a range where it is unclear what it measures), do not use it for the headline or the main claim: pick another fact.
- For any percentage, ratio or range, say exactly what it measures and use cautious wording in the caption ("up to", "about", "according to measurements").

HEADLINE RULES:
- headline_lines: 2 or 3 ALL-CAPS lines forming ONE short curiosity hook, 8-12 words in total, each line MAX 28 characters. It teases a surprise, it does NOT retell the whole fact. Never name living people.
- Examples: ["A [3] HEARTED ANIMAL", "THAT NEVER STOPS", "CHANGING COLOUR"] or ["IT CAN SMELL FOOD", "[40 KM] AWAY"].
- Red highlight: wrap ONE key word or number (two short groups at most) in [square brackets]. The brackets must hug the words and punctuation stays OUTSIDE ("[40 KM],", never "[40 KM,]"). Never put a whole line in brackets. Do not use parentheses.
- subhook: 3-6 words, ALL CAPS, a short teaser shown under the headline. It adds NO new fact (for example "AND IT DOES IT EVERY DAY").
- The headline must be truthful and supported by the source.

CAPTION RULES (write like a real person, not like an AI):
- 150-260 words (count them: a caption under 150 words is rejected), plain American English, in the voice of ONE sharp, slightly opinionated nature nerd telling a friend about an animal that stunned him. The user message gives you a VOICE and a SHAPE: follow them and no other template.
- Start with the surprising thing itself (an odd detail, a number, what the animal actually does). The first sentence is what Facebook shows before "See more": it must work alone, under 120 characters, and hold the surprising fact. The animal's name and where it lives (its region or habitat) must both appear within the first two sentences. Never start with "Picture", "Imagine", "Meet the", "Welcome to", "In a world", "Have you ever", or a scene-setting opener.
- Mix very short sentences (2-6 words) with long winding ones (25+ words). Break the rhythm on purpose. Fragments are fine. Use contractions. One small aside is fine.
- Use the odd, specific words of the SOURCE TEXT (names of body parts, prey, places, behaviours) instead of general words. Say what the animal actually does: digs, dives, hunts, builds, hides, hauls.
- Never write vague summary lines such as "marked a turning point", "played a crucial role", "paved the way", "highlighting", "underscores", "a testament to", "stands as", "boasts", "a haven for", "nature's wonder", "fascinating creature", "breathtaking", "stunning", "vibrant", "rich tapestry". Never use the words: delve, tapestry, testament, realm, pivotal, crucial, moreover, furthermore, additionally, notably, ultimately; never write "it's worth noting", "in conclusion", "not just ... but", "Here's the thing", "fun fact".
- No lists of three adjectives or three parallel clauses. No moral, no summary, no lesson, no sales pitch.
- 3 to 6 paragraphs of UNEVEN length, separated by a blank line (write \\n\\n inside the JSON string). One paragraph may be one short sentence.
- Ending: ONE of these, never two: a blunt last line, a small joke, or one short question. Never two questions in the text. Never ask people to like, share, comment, tag or follow.
- Never fake first-hand experience: no "I saw", no invented quotes from scientists, rangers or visitors. You may have opinions about the facts.
- Facts: keep every number, unit and name exactly as in the SOURCE TEXT. Own words: never copy six words in a row from the SOURCE TEXT.
- Plain text only. No em dashes, no emojis, no markdown, no hashtags inside the text.

TOPIC RULE:
- The story must be about a real animal species or animal group, its body, senses, behaviour, life cycle, habitat or survival.
- NEVER choose topics about animal cruelty, hunting for sport, graphic killing, gore, animal abuse, bestiality or anything sexual or disturbing. Predation may be mentioned briefly and without detail.
- Do not write about pets as medical advice. No health or feeding advice for pets.
- Do not repeat the famous myths and cliches: {banned}.

JSON FORMAT: output valid JSON only. Inside string values write paragraph breaks as the escaped characters \n (never real line breaks) and write every double quote inside text as \" . No comments, no trailing commas.

Return ONLY one JSON object, no markdown fences, with keys:
skip, topic, fact, habitat (a short place such as "Australia" or "deep ocean"), headline_lines (array), subhook, image_prompt_a, image_prompt_b, caption, hashtags (array of exactly 2 relevant topical hashtags like "#WildAnimals"), evidence (array described above)."""
WRITER_SYSTEM = WRITER_SYSTEM.replace("{banned}", BANNED)

CHECKER_SYSTEM = """You are a strict fact checker. You receive SOURCE TEXT (from Wikipedia) and a DRAFT social post.
Decide whether every factual claim in the draft's headline and caption is supported by the SOURCE TEXT.
Flag ONLY hard factual problems: a number, date, amount, name or event that is not in the source; wrong cause and effect; "first/only/largest/fastest/deadliest" style claims the source does not make; numbers or units attached to the wrong thing; a different species than the one in the source.
Also flag a percentage, ratio or range whose meaning was changed or made more dramatic than the source sentence (for example "up to 80 km/h in short bursts" retold as "runs at 80 km/h").
Do NOT flag rhetorical questions, metaphors, transitions, mood, short summaries of what the source says, or the closing reflection. Wording like "simple" or "brutal" is not a factual claim unless it changes the facts.
Do NOT use outside knowledge to approve a claim. If the source does not say it, it is unsupported.
Return ONLY one JSON object, no markdown fences:
{"faithful": true or false, "unsupported": ["..."], "hook_score": 1-10}
hook_score rates how strongly the headline would stop a Facebook scroller (10 = jaw-dropping specific number and contrast, 1 = vague textbook summary)."""

CHECKER_WEB_SYSTEM = CHECKER_SYSTEM.replace(
    "Do NOT use outside knowledge to approve a claim. If the source does not say it, it is unsupported.",
    "You also have web search. Confirm the key numbers, dates and names of the headline with reliable web sources "
    "(zoos, natural history museums, universities, IUCN, national park sites, reputable encyclopedias). "
    "If a reliable source contradicts the SOURCE TEXT on a number, date or name, mark the draft unfaithful and list the contradiction. "
    "A claim must still appear in the SOURCE TEXT to be approved.",
)

STYLE_SUFFIX = (
    "professional wildlife photograph, shot with a 400mm telephoto lens, natural golden light, "
    "sharp eyes, true-to-life colours, shallow depth of field, National Geographic style"
)
NEGATIVE_PROMPT = (
    "deformed animal, wrong anatomy, extra legs, extra heads, extra eyes, missing limbs, fused bodies, melted face, "
    "blurry, low resolution, text, letters, watermark, logo, cartoon, painting, illustration, 3d render, "
    "oversaturated, human, person, hands, cage"
)


# ----------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def load_history():
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(items):
    HISTORY.write_text(json.dumps(items[-200000:], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _json_candidates(text):
    """Yield every balanced top-level {...} block, ignoring braces inside strings."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, j = 0, False, False, i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[i : j + 1]
                    break
            j += 1
        i = j + 1 if depth == 0 and j < n else i + 1


def _parse_loose(candidate):
    attempts = [candidate]
    attempts.append(re.sub(r",\s*([}\]])", r"\1", candidate))  # trailing commas
    attempts.append(attempts[-1].replace("\u201c", '"').replace("\u201d", '"'))  # smart quotes used as delimiters
    for c in attempts:
        try:
            return json.loads(c, strict=False)  # strict=False allows raw line breaks inside strings
        except ValueError:
            continue
    return None


def extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    if "{" not in text:
        raise ValueError("no JSON found")
    good = None
    for cand in _json_candidates(text):
        obj = _parse_loose(cand)
        if isinstance(obj, dict) and obj:
            good = obj  # keep the LAST valid object (final answer after any draft)
    if good is None:
        raise ValueError("invalid JSON (could not repair)")
    return good


# ---- OpenRouter key pool: many keys, round-robin, exhausted keys are parked automatically
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
KEYSTATE = HERE / "keystate.json"
WEB_CHECK = os.environ.get("WEB_CHECK", "0") == "1"


def _read_keys():
    raw = os.environ.get("OPENROUTER_API_KEYS", "") or os.environ.get("OPENROUTER_API_KEY", "")
    return [k.strip() for k in re.split(r"[\s,;]+", raw) if k.strip()]


OR_KEYS = _read_keys()
_KS = {"rr": 0, "keys": {}}


def _fp(key):
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def load_keystate():
    """Nothing is remembered between runs: a key that was tired yesterday gets a fresh chance today."""
    global _KS
    _KS = {"rr": random.randrange(1000), "keys": {}, "demoted": {}}


def save_keystate():
    pass


def _parked(fp):
    return _KS["keys"].get(fp, {}).get("dead_until", 0) > time.time()


def _park(fp, seconds, reason):
    e = _KS["keys"].setdefault(fp, {})
    e["dead_until"] = int(time.time() + seconds)
    e["reason"] = reason


def _next_midnight():
    """Seconds until the next UTC midnight (+5 min), when OpenRouter's daily free limit resets."""
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return (nxt - now).total_seconds()


def keys_usable():
    return [k for k in OR_KEYS if not _parked(_fp(k))]


class ModelBusy(RuntimeError):
    """The model's provider is overloaded or rate-limited upstream. The key is fine; try another model."""


_BUSY = {}
_REASONING_MODELS = set()   # models whose catalogue entry supports a reasoning parameter
_NO_REASON_PARAM = set()   # models that rejected that parameter


# ---- Cloudflare Workers AI for the TEXT too (writer, checker, human-voice editor): same free 10,000 neurons a day as the pictures
CF_TEXT_MODEL = os.environ.get("CF_TEXT_MODEL", "").strip() or "@cf/google/gemma-4-26b-a4b-it"
CF_CHECKER_MODEL = os.environ.get("CF_CHECKER_MODEL", "").strip() or "@cf/qwen/qwen3-30b-a3b-fp8"
CF_TEXT = os.environ.get("CF_TEXT", "1").strip() == "1" and bool(CF_ACCOUNT_ID and CF_API_TOKEN)

# neurons per million tokens (input, output) from the Workers AI pricing page; used only for the usage estimate in the log
CF_RATES = {
    "@cf/google/gemma-4-26b-a4b-it": (9091, 27273),
    "@cf/qwen/qwen3-30b-a3b-fp8": (4625, 30475),
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast": (4119, 34868),
    "@cf/openai/gpt-oss-20b": (18182, 27273),
    "@cf/openai/gpt-oss-120b": (31818, 68182),
}
_NEURONS = {"text": 0.0, "image": 0.0, "calls": 0}
_CF_KWARGS_REJECTED = [False]


def cf_call(messages, model, temperature=0.3, max_tokens=800):
    """One Workers AI chat request through the OpenAI-compatible endpoint."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1/chat/completions"
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if not _CF_KWARGS_REJECTED[0]:
        body["chat_template_kwargs"] = {"enable_thinking": False}  # we want the answer, not paid hidden thinking
    last = None
    for _ in range(2):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"},
                              json=body, timeout=150)
        except requests.RequestException as e:
            raise ModelBusy(f"{model} network error: {e}")
        text = r.text[:300]
        low = text.lower()
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError(f"model rejected the request: not JSON: {text[:120]}")
            if isinstance(data, dict) and isinstance(data.get("result"), dict) and "choices" in data["result"]:
                data = data["result"]
            ch = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
            msg = ch.get("message") or {}
            content = msg.get("content") or ""
            usage = data.get("usage") or {} if isinstance(data, dict) else {}
            pt = usage.get("prompt_tokens") or sum(len(str(m.get("content", ""))) for m in messages) // 4
            ct = usage.get("completion_tokens") or len(content) // 4
            rin, rout = CF_RATES.get(model, (9091, 27273))
            n = pt * rin / 1e6 + ct * rout / 1e6
            _NEURONS["text"] += n
            _NEURONS["calls"] += 1
            log(f"[cloudflare] {model} ok ({pt}+{ct} tokens, about {n:.0f} neurons)")
            if not content.strip():
                if ch.get("finish_reason") == "length":
                    raise RuntimeError(f"model rejected: {model} spent the whole {max_tokens}-token limit on hidden reasoning")
                content = msg.get("reasoning_content") or msg.get("reasoning") or ""
                if not content.strip():
                    raise RuntimeError(f"model rejected: {model} returned an empty answer")
            return content
        last = f"HTTP {r.status_code}: {text[:200]}"
        log(f"[cloudflare] {model} failed: {last}")
        if r.status_code == 429 or "neurons" in low or "daily free allocation" in low or "4006" in low:
            if "neurons" in low or "allocation" in low or "4006" in low:
                _BUSY[model] = time.time() + 86400
                raise ModelBusy(f"{model}: Workers AI daily free allocation used up")
            _BUSY[model] = time.time() + 120
            raise ModelBusy(f"{model} is rate limited ({last[:90]})")
        if r.status_code in (500, 502, 503, 504):
            _BUSY[model] = time.time() + 300
            raise ModelBusy(f"{model} is busy ({last[:90]})")
        if r.status_code == 400 and "chat_template_kwargs" in body and ("chat_template" in low or "enable_thinking" in low or "unknown" in low or "extra" in low):
            _CF_KWARGS_REJECTED[0] = True
            body.pop("chat_template_kwargs", None)
            log("[cloudflare] the thinking switch was rejected, retrying without it")
            continue
        raise RuntimeError(f"model rejected the request: {last}")
    raise RuntimeError(f"model rejected the request: {last}")


def or_call(messages, model, temperature=0.3, max_tokens=800, plugins=None):
    """One OpenRouter request. Starts at the next key in the rotation and moves on if a key is out of credit."""
    if model.startswith("@cf/"):
        return cf_call(messages, model, temperature, max_tokens)
    if not OR_KEYS:
        raise RuntimeError("no OpenRouter keys configured (secret OPENROUTER_API_KEYS)")
    usable = keys_usable()
    if not usable:  # never give up without trying: forget the rests and use every key again
        log("all OpenRouter keys were resting; trying them all again")
        _KS["keys"].clear()
        usable = keys_usable()
    start = _KS["rr"] % len(usable)
    _KS["rr"] += 1
    order = usable[start:] + usable[:start]
    last = None
    for key in order[:10]:
        fp = _fp(key)
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if model in _REASONING_MODELS and model not in _NO_REASON_PARAM:
            body["reasoning"] = {"enabled": False}  # we want the answer, not pages of hidden thinking
        if plugins:
            body["plugins"] = plugins
        try:
            r = requests.post(
                OR_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "HTTP-Referer": "https://github.com", "X-Title": PAGE_NAME},
                json=body,
                timeout=150,
            )
        except requests.RequestException as e:
            last = f"network error: {e}"
            log(f"[openrouter {fp}] {last}")
            continue
        try:
            data = r.json()
        except ValueError:
            data = {}
        err = data.get("error") if isinstance(data, dict) else None
        code = r.status_code
        if err and not data.get("choices") and str(err.get("code", "")).isdigit():
            code = int(err["code"])
        if code == 200 and isinstance(data, dict) and data.get("choices"):
            usage = data.get("usage") or {}
            log(f"[openrouter {fp}] {model} ok ({usage.get('total_tokens', '?')} tokens)")
            if fp in _KS["keys"]:
                _KS["keys"][fp]["r429"] = 0
            ch = data["choices"][0]
            msg = ch.get("message") or {}
            content = msg.get("content") or ""
            if not content.strip() and ch.get("finish_reason") == "length":
                raise RuntimeError(f"model rejected: {model} spent the whole {max_tokens}-token limit on hidden reasoning")
            if not content.strip():
                reasoning = msg.get("reasoning") or ""
                log(f"[openrouter {fp}] {model} returned EMPTY content (finish_reason={ch.get('finish_reason')}, "
                    f"reasoning_chars={len(reasoning)}); if finish_reason is 'length' the token limit was too small")
                content = reasoning  # some reasoning models leave the final JSON in the reasoning field
            elif ch.get("finish_reason") == "length":
                log(f"[openrouter {fp}] {model} was cut off at the token limit")
            return content
        text = str(err or r.text)[:200]
        last = f"HTTP {code}: {text}"
        log(f"[openrouter {fp}] {model} failed: {last}")
        low = text.lower()
        meta = (err or {}).get("metadata") if isinstance(err, dict) else None
        etype = str((meta or {}).get("error_type", "")).lower() if isinstance(meta, dict) else ""
        if ("provider returned error" in low or "upstream" in low or "overloaded" in low or "provider_overloaded" in etype
                or code in (502, 503, 504)):
            _BUSY[model] = time.time() + 600
            raise ModelBusy(f"{model} is busy upstream ({last[:90]})")
        if code == 402:
            _park(fp, _next_midnight(), "no credit (try again tomorrow)")
            log(f"[openrouter {fp}] no credit, key rests until the next UTC day")
        elif code == 401 or (code == 403 and "moderation" not in low and "flagged" not in low):
            _park(fp, 30 * 86400, "invalid or forbidden key")
        elif code == 429:
            e = _KS["keys"].setdefault(fp, {})
            e["r429"] = e.get("r429", 0) + 1
            if "per-day" in low or "per day" in low or "daily" in low or e["r429"] >= 3:
                e["r429"] = 0
                _park(fp, _next_midnight(), "daily limit reached (resets at UTC midnight)")
                log(f"[openrouter {fp}] daily limit reached, key rests until the next UTC day")
            else:
                _park(fp, 120, "rate limited for 2 minutes")
        elif code == 400 and "reasoning" in body and "reasoning" in low:
            _NO_REASON_PARAM.add(model)  # this model does not accept the switch: retry without it
            log(f"[{model}] does not accept the reasoning switch, retrying without it")
            continue
        elif code in (400, 403, 404):
            raise RuntimeError(f"model rejected the request: {last}")
        time.sleep(0.5)
    raise RuntimeError(f"all tried OpenRouter keys failed: {last}")


def chat(system, user, model, temperature=0.9, max_tokens=3000, plugins=None):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return or_call(messages, model, temperature, max_tokens, plugins)


# ---- automatic choice of the best FREE models (no credit is spent unless you enable a paid fallback)
FAMILY_BONUS = {"deepseek": 30, "qwen": 26, "gpt-oss": 25, "glm": 24, "kimi": 24, "llama": 20, "mistral": 14, "gemma": 12, "nvidia": 12}
FAMILY_DEFAULT_SIZE = {"deepseek": 600, "kimi": 1000, "glm": 300, "qwen": 100, "gpt-oss": 120, "llama": 70, "mistral": 24, "gemma": 27, "nvidia": 50}
SKIP_NAME = re.compile(r"guard|safety|moderat|embed|tts|audio|whisper|image|diffusion|ocr|coder|code|roleplay|uncensor", re.I)
SLOW_NAME = re.compile(r"r1|reason|think", re.I)
STATIC_FREE = [
    "deepseek/deepseek-chat-v3.1:free", "qwen/qwen3-235b-a22b:free", "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free", "mistralai/mistral-small-3.2-24b-instruct:free",
]


def _family(model):
    mid = model.lower()
    for fam in FAMILY_BONUS:
        if fam in mid:
            return fam
    return mid.split("/")[0]


def _score(m):
    mid = m["id"].lower()
    sizes = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)b", mid)]
    fam = _family(mid)
    size = max(sizes) if sizes else FAMILY_DEFAULT_SIZE.get(fam, 20)
    size_score = min(math.log2(size + 1) * 6, 50)
    if fam not in FAMILY_BONUS:  # unknown lab: trust a big number in the name less
        size_score *= 0.6
    score = FAMILY_BONUS.get(fam, 0) + size_score
    score += min(max((m.get("created", 0) - 1735689600) / (30 * 86400), 0) * 0.5, 20)  # newer is better (months since 2025-01)
    if "instruct" in mid or "chat" in mid:
        score += 3
    if SLOW_NAME.search(mid) or m.get("reasoning"):
        score -= 20
    elif m.get("rparam"):
        score -= 6  # hybrid models: usable with thinking switched off, but plain instruct models are safer
    for pref in _models("PREFER_MODELS", ""):
        if pref.lower() in mid:
            score += 100
    return score


def discover_models():
    """Ask OpenRouter which models are free right now and rank them; falls back to a static list."""
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=60)
        r.raise_for_status()
        free = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            pr = m.get("pricing") or {}
            try:
                is_free = float(pr.get("prompt", 1)) == 0 and float(pr.get("completion", 1)) == 0
            except (TypeError, ValueError):
                is_free = False
            if not (is_free or mid.endswith(":free")) or mid.startswith("openrouter/") or SKIP_NAME.search(mid):
                continue
            arch = m.get("architecture") or {}
            if (arch.get("output_modalities") or ["text"]) != ["text"]:
                continue
            exp = m.get("expiration_date")
            if exp:
                try:
                    if datetime.fromisoformat(str(exp)[:10]).replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) + timedelta(days=14):
                        continue  # going away soon
                except ValueError:
                    pass
            desc = str(m.get("description") or "").lower()
            if re.search(r"coding agent|coding model|code generation|software engineering agent|agentic coding", desc):
                continue
            ctx = m.get("context_length") or 0
            if ctx and ctx < 16000:
                continue
            free.append({"id": mid, "created": m.get("created") or 0, "vision": "image" in (arch.get("input_modalities") or []),
                         "reasoning": "reasoning" in desc,
                         "rparam": "reasoning" in (m.get("supported_parameters") or []) or "include_reasoning" in (m.get("supported_parameters") or [])})
            if "reasoning" in (m.get("supported_parameters") or []) or "include_reasoning" in (m.get("supported_parameters") or []):
                _REASONING_MODELS.add(mid)
        if free:
            free.sort(key=_score, reverse=True)
            return [m["id"] for m in free], [m["id"] for m in free if m["vision"]]
    except Exception as e:  # noqa: BLE001
        log("could not read the OpenRouter model list:", e)
    return list(STATIC_FREE), []


def _demoted(model):
    return _KS.setdefault("demoted", {}).get(model, 0) > time.time()


def _demote(model, seconds):
    _KS.setdefault("demoted", {})[model] = int(time.time() + seconds)
    log(f"model {model} rests for {seconds // 3600} h")


def setup_models():
    global WRITER_MODELS, CHECKER_MODELS, QA_MODELS
    ranked, vision = ([], [])
    if OR_KEYS:
        ranked, vision = discover_models()
        ranked = [m for m in ranked if not _demoted(m)]
    paid = _models("PAID_FALLBACK_MODEL", "")  # optional: only used if every free model fails
    cf_w = [CF_TEXT_MODEL] if CF_TEXT else []
    cf_c = [CF_CHECKER_MODEL] if CF_TEXT else []
    if not WRITER_MODELS:
        WRITER_MODELS = list(dict.fromkeys(cf_w + ranked[:6] + paid))
    if not CHECKER_MODELS:
        fam = _family(ranked[0]) if ranked else ""
        others = [m for m in ranked if _family(m) != fam][:4]
        CHECKER_MODELS = list(dict.fromkeys(cf_c + (others or ranked[:4]) + ranked[:2] + paid))
    if not QA_MODELS:
        QA_MODELS = [m for m in vision if not _demoted(m)][:3]
    log("writer models:", WRITER_MODELS[:3], "| checker models:", CHECKER_MODELS[:3], "| image QA models:", QA_MODELS[:2])


def chat_json(system, user, models, temperature, tries=2, max_tokens=3000, plugins=None):
    last = None
    for model in models:
        if _BUSY.get(model, 0) > time.time() and len(models) > 1:
            continue  # busy upstream a moment ago, use the next model first
        bad = 0
        for i in range(tries):
            try:
                raw = chat(system, user, model, temperature, max_tokens, plugins)
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"[{model}] call failed: {e}")
                if str(e).startswith("model rejected"):
                    _demote(model, 24 * 3600)
                break  # go to next model
            try:
                return extract_json(raw)
            except ValueError as e:
                last = e
                bad += 1
                flat = (raw or "").strip().replace("\n", " ")
                snippet = flat[:240] + (" ... " + flat[-120:] if len(flat) > 400 else flat[240:])
                log(f"[{model}] bad JSON ({i + 1}/{tries}): {e} | reply length {len(flat)} | reply was: {snippet!r}")
        if bad >= tries:
            _demote(model, 6 * 3600)
    raise RuntimeError(f"no model returned valid JSON: {last}")


# ---- free evidence: Wikipedia (no API key, no cost)
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_UA = os.environ.get("WIKI_UA", "WildloreFactsBot/1.0 (educational animal-facts page; GitHub Actions)")
SHOW_SOURCE = os.environ.get("SHOW_SOURCE", "0") == "1"
BANNED_TITLE = re.compile(r"^domestic|^dog$|^cat$|breed|pet food|taxidermy|zoophilia|bestiality", re.I)
CUT_HEADINGS = re.compile(r"\n=+ *(References|See also|External links|Notes|Further reading|Bibliography|Footnotes|Sources|Citations) *=+", re.I)
YEAR_RE = re.compile(r"\b(?:1[5-9]\d\d|20\d\d)\b")
NUM_RE = re.compile(
    r"([$\u00a3\u20ac])?\s?(\d[\d,]*(?:\.\d+)?)(s)?\b\s?(kg|kilograms?|grams?|g|tonnes?|tons?|pounds?|lbs?|ounces?|km|kilometres?|kilometers?|metres?|meters?|m|cm|centimetres?|centimeters?|mm|millimetres?|miles?|feet|foot|ft|inch(?:es)?|mph|km/h|hours?|minutes?|days?|weeks?|months?|years?|species|eggs?|hearts?|legs?|teeth|degrees?|percent|%)?",
    re.I,
)


_WIKI_LAST = [0.0]


def wiki_get(params):
    """Polite, rate-limit friendly Wikipedia call: spaced requests and patient retries on 429/503."""
    p = {"format": "json", "formatversion": "2", **params}
    last = None
    for i in range(5):
        wait = 1.3 - (time.time() - _WIKI_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _WIKI_LAST[0] = time.time()
        r = requests.get(WIKI_API, params=p, headers={"User-Agent": WIKI_UA, "Accept-Encoding": "gzip"}, timeout=30)
        if r.status_code in (429, 503):
            try:
                delay = float(r.headers.get("Retry-After", ""))
            except ValueError:
                delay = 0
            delay = max(delay, 8 * (i + 1))
            last = f"HTTP {r.status_code}"
            log(f"wikipedia busy ({r.status_code}), waiting {delay:.0f}s")
            time.sleep(min(delay, 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"wikipedia kept refusing: {last}")


def wiki_search(query, limit=10):
    data = wiki_get({"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "srnamespace": 0})
    return [x["title"] for x in data.get("query", {}).get("search", [])]


def wiki_extract(title):
    data = wiki_get({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": title})
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return ""
    text = pages[0].get("extract", "") or ""
    m = CUT_HEADINGS.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()[:14000]


SHOW_SUBHOOK = os.environ.get("SHOW_SUBHOOK", "1") == "1"
STRICT_RULES = os.environ.get("STRICT_RULES", "0") == "1"   # 1 = copy/structure/percentage rules reject posts
SENSITIVE_TITLE = re.compile(
    r"sexual|porn|genital|rape|bestiality|zoophilia|slaughter|cruelty|abuse|torture|bullfight|cockfight|dog fighting|"
    r"trophy hunting|whaling|dog meat|euthanasia|factory farm|animal testing|vivisection|cull",
    re.I,
)


CURATED_TOPICS = [
    "African elephant", "Asian elephant", "Blue whale", "Sperm whale", "Humpback whale", "Orca",
    "Bottlenose dolphin", "Narwhal", "Beluga whale", "Sea otter", "Giant otter", "Polar bear",
    "Brown bear", "Giant panda", "Sloth", "Three-toed sloth", "Pangolin", "Aardvark",
    "Armadillo", "Platypus", "Echidna", "Koala", "Red kangaroo", "Tasmanian devil",
    "Wombat", "Quokka", "Cheetah", "Lion", "Tiger", "Leopard",
    "Snow leopard", "Jaguar", "Wolf", "African wild dog", "Hyena", "Spotted hyena",
    "Meerkat", "Honey badger", "Wolverine", "Fennec fox", "Arctic fox", "Red fox",
    "Giraffe", "Okapi", "Hippopotamus", "Rhinoceros", "Black rhinoceros", "Zebra",
    "Moose", "Reindeer", "Camel", "Bactrian camel", "Gorilla", "Chimpanzee",
    "Bonobo", "Orangutan", "Capuchin monkey", "Mandrill", "Proboscis monkey", "Aye-aye",
    "Ring-tailed lemur", "Tarsier", "Vampire bat", "Fruit bat", "Naked mole-rat", "Capybara",
    "Beaver", "Porcupine", "Star-nosed mole", "Elephant seal", "Walrus", "Manatee",
    "Dugong", "Bat", "Common shrew", "Etruscan shrew", "Saiga antelope", "Pronghorn",
    "Bison", "Yak", "Musk ox", "Tapir", "Peregrine falcon", "Bald eagle",
    "Harpy eagle", "Golden eagle", "Barn owl", "Snowy owl", "Great horned owl", "Emperor penguin",
    "King penguin", "Albatross", "Wandering albatross", "Arctic tern", "Bar-tailed godwit", "Hummingbird",
    "Bee hummingbird", "Ostrich", "Emu", "Cassowary", "Kiwi (bird)", "Kakapo",
    "Lyrebird", "Bowerbird", "Birds-of-paradise", "Toucan", "Hornbill", "Pelican",
    "Flamingo", "Common swift", "Crow", "New Caledonian crow", "Raven", "Magpie",
    "African grey parrot", "Kea", "Shoebill", "Secretarybird", "Sandgrouse", "Woodpecker",
    "Cuckoo", "Weaver bird", "Komodo dragon", "Saltwater crocodile", "Nile crocodile", "American alligator",
    "Gharial", "Galápagos tortoise", "Leatherback sea turtle", "Green sea turtle", "Chameleon", "Panther chameleon",
    "Gecko", "Thorny devil", "Frilled-neck lizard", "Basilisk lizard", "King cobra", "Black mamba",
    "Anaconda", "Green anaconda", "Reticulated python", "Rattlesnake", "Inland taipan", "Poison dart frog",
    "Golden poison frog", "Axolotl", "Glass frog", "Wood frog", "Cane toad", "Hellbender",
    "Great white shark", "Whale shark", "Hammerhead shark", "Greenland shark", "Goblin shark", "Manta ray",
    "Electric eel", "Anglerfish", "Seahorse", "Clownfish", "Pufferfish", "Mola mola",
    "Salmon", "Atlantic salmon", "Bluefin tuna", "Sailfish", "Swordfish", "Lamprey",
    "Hagfish", "Mudskipper", "Giant squid", "Colossal squid", "Octopus", "Giant Pacific octopus",
    "Mimic octopus", "Blue-ringed octopus", "Cuttlefish", "Nautilus", "Box jellyfish", "Immortal jellyfish",
    "Mantis shrimp", "Pistol shrimp", "Horseshoe crab", "Coconut crab", "Giant isopod", "Sea star",
    "Sea cucumber", "Giant clam", "Barnacle", "Coral", "Monarch butterfly", "Honey bee",
    "Bumblebee", "Leafcutter ant", "Army ant", "Bullet ant", "Weaver ant", "Termite",
    "Dragonfly", "Praying mantis", "Firefly", "Dung beetle", "Hercules beetle", "Bombardier beetle",
    "Cicada", "Mayfly", "Locust", "Atlas moth", "Peacock spider", "Jumping spider",
    "Goliath birdeater", "Trapdoor spider", "Scorpion", "Tardigrade", "Earthworm", "Slug",
    "Snail", "Giant African land snail", "Flea", "Mosquito",
]
_TRIED = set()
_LAST_SOURCE = []   # the last article we fetched, used by the emergency post


# Wikipedia category roots: the bot walks down these trees at random, so the supply of animals is practically endless
ROOT_CATEGORIES = [
    "Category:Mammals", "Category:Birds", "Category:Reptiles", "Category:Amphibians", "Category:Fish",
    "Category:Insects", "Category:Arachnids", "Category:Crustaceans", "Category:Molluscs", "Category:Cephalopods",
    "Category:Cnidarians", "Category:Sharks", "Category:Rays", "Category:Whales", "Category:Dolphins", "Category:Primates",
    "Category:Carnivorans", "Category:Rodents", "Category:Bats", "Category:Marsupials", "Category:Ungulates",
    "Category:Snakes", "Category:Lizards", "Category:Turtles", "Category:Crocodilians", "Category:Frogs",
    "Category:Owls", "Category:Parrots", "Category:Penguins", "Category:Birds of prey", "Category:Seabirds",
    "Category:Beetles", "Category:Butterflies", "Category:Moths", "Category:Ants", "Category:Bees", "Category:Spiders",
    "Category:Deep sea fish", "Category:Venomous animals", "Category:Flightless birds", "Category:Migratory animals",
]
SKIP_CATEGORY = re.compile(
    r"in culture|in popular|in fiction|mythology|folklore|symbols|stubs|lists?\b|by country|by continent|by region|"
    r"people|researchers|taxonomists|zoologists|biologists|breeds|domestic|livestock|pets|hunting|fishing|food|cuisine|"
    r"parasites of humans|diseases|fossil|prehistoric|extinct|taxa named|described in|articles|images|wikipedia|"
    r"conservation|organisations|organizations|museums|zoos|aquariums|sports|mascots|films|television|literature|games",
    re.I,
)
NON_ANIMAL_TITLE = re.compile(
    r"\((?:[a-z]+ )?(?:whale|dolphin|dog|cat|horse|elephant|gorilla|chimpanzee|bear|lion|tiger|seal|orca|parrot|crow|cow|pig|"
    r"sheep|goat|bird|fish|shark|turtle|tortoise|snake|monkey|ape|bull|ox|mule|donkey|camel|owl|eagle|penguin|otter|walrus)\)$"   # one famous individual animal
    r"|declaration|treaty|convention|\bact\b|society|foundation|project\b|institute|museum|\bzoo\b|aquarium|sanctuary|reserve|national park|"
    r"ology$|ologist|terminology|anatomy|pollination|taxonomy|classification|skeleton|tourism|\bdiet\b",
    re.I,
)
TAXON_WORDS = re.compile(r"\b(species|genus|subspecies|family|subfamily|order|suborder|clade|superfamily|tribe)\b", re.I)
SCI_NAME = re.compile(r"\([A-Z][a-z]+ [a-z]+(?: [a-z]+)?[,;)]")


def looks_like_animal(title, text):
    """A category walk can land on laws, museums, individual animals or biology terms. Keep real animal species/groups only."""
    if NON_ANIMAL_TITLE.search(title):
        return False
    lead = text[:900]
    return bool(TAXON_WORDS.search(lead) or SCI_NAME.search(lead))


_CAT_CACHE = {}


def _category_members(cat):
    if cat in _CAT_CACHE:
        return _CAT_CACHE[cat]
    data = wiki_get({"action": "query", "list": "categorymembers", "cmtitle": cat, "cmlimit": 500, "cmtype": "page|subcat"})
    pages, subs = [], []
    for m in data.get("query", {}).get("categorymembers", []):
        t = m.get("title", "")
        if m.get("ns") == 14:
            if not SKIP_CATEGORY.search(t):
                subs.append(t)
        elif m.get("ns") == 0:
            if not re.match(r"(List of|Timeline of|Index of|Outline of)", t) and not BANNED_TITLE.search(t) and not SENSITIVE_TITLE.search(t) \
                    and not NON_ANIMAL_TITLE.search(t):
                pages.append(t)
    _CAT_CACHE[cat] = (pages, subs)
    return pages, subs


def crawl_animal(used):
    """Random walk down the Wikipedia animal category tree until it lands on an article that was not used before."""
    for _ in range(4):
        cat = random.choice(ROOT_CATEGORIES)
        goal = random.randint(2, 6)   # how deep this walk goes
        for depth in range(goal + 3):
            try:
                pages, subs = _category_members(cat)
            except Exception as e:  # noqa: BLE001
                log("category walk failed:", str(e)[:100])
                break
            fresh = [t for t in pages if t not in used and t not in _TRIED]
            if fresh and (not subs or depth >= goal or random.random() < 0.35):
                return random.choice(fresh)
            if not subs:
                break
            cat = random.choice(subs)
    return None


def pick_source(used, level=0):
    """A real Wikipedia article about an animal. Unlimited supply: a mix of hand-picked titles and a random walk
    through Wikipedia's animal categories. A keyword search is only the last backup."""
    pool = [t for t in CURATED_TOPICS if t not in used and t not in _TRIED]
    for _ in range(3):
        if pool and random.random() < 0.25:
            t = random.choice(pool)
            pool.remove(t)
            kind = "curated"
        else:
            t = crawl_animal(used)
            kind = "category walk"
        if not t:
            continue
        _TRIED.add(t)
        text = wiki_extract(t)
        need = 1500 if kind == "curated" else 3000   # thin articles give thin posts
        if len(text) >= need and (kind == "curated" or looks_like_animal(t, text)):
            log(f"article {t!r} accepted ({kind}, {len(text)} characters)")
            _LAST_SOURCE[:] = [t, text]
            return t, text
        why = "not found or too short" if len(text) < need else "not an animal species/group article"
        log(f"skipped {t!r} ({kind}): {why}")
    cat = random.randrange(len(CATEGORIES))
    query = random.choice(CATEGORY_KEYWORDS[cat])
    log("backup search:", query)
    titles = [
        t for t in wiki_search(query, 15)
        if not re.match(r"(List of|Timeline of|Index of|Outline of)", t) and not BANNED_TITLE.search(t)
        and not SENSITIVE_TITLE.search(t) and t not in used and t not in _TRIED
    ][:6]
    for t in titles[:3]:
        _TRIED.add(t)
        text = wiki_extract(t)
        if len(text) >= 1500:
            log(f"article {t!r} accepted (search, {len(text)} characters)")
            _LAST_SOURCE[:] = [t, text]
            return t, text
    raise ValueError("no usable article this time")


def _norm(s):
    s = s.lower()
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$\u00a3\u20ac%.' -]", " ", s)).strip()


def quote_supported(quote, src_norm, src_tokens):
    q = _norm(quote)
    qt = q.split()
    if len(qt) < 4:
        return False
    if q in src_norm:
        return True
    qset, n = set(qt), len(qt)
    for i in range(0, max(1, len(src_tokens) - n + 1)):
        if len(qset & set(src_tokens[i : i + n + 3])) / len(qset) >= 0.9:
            return True
    return False


def numbers_in(text, factual_only):
    out = set()
    for m in NUM_RE.finditer(text):
        cur, num, decade, unit = m.group(1), m.group(2), m.group(3), m.group(4)
        n = num.replace(",", "").rstrip(".")
        if not n:
            continue
        if not factual_only:
            out.add(n)
            continue
        if decade:  # "1930s" style decades are not exact claims
            continue
        try:
            val = float(n)
        except ValueError:
            continue
        if cur or unit or YEAR_RE.fullmatch(n) or val >= 100:
            out.add(n)
    return out


def verify_grounding(post, source):
    """Deterministic check: every quote must be in the source and every key number must come from it."""
    problems = []
    evidence = post.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < 2:
        return ["evidence missing"]
    src_norm = _norm(source)
    src_tokens = src_norm.split()
    for item in evidence:
        quote = item.get("quote", "") if isinstance(item, dict) else str(item)
        if not quote_supported(quote, src_norm, src_tokens):
            problems.append(f"quote not found in source: {quote[:70]!r}")
    text = " ".join(post["headline_lines"]) + " " + post["caption"]
    missing = numbers_in(text, True) - numbers_in(source, False)
    if missing:
        problems.append(f"numbers not in source: {sorted(missing)}")
    return problems


VOICES = [
    "dry and wry, with one deadpan aside",
    "warm and curious, like a storyteller at a pub",
    "blunt and matter-of-fact, with a little sarcasm",
    "amused and a bit incredulous",
    "calm and precise, with one vivid comparison",
]
SHAPES = [
    "Open with the number or the oddest detail, then back up and explain how it came about, and end on a blunt line.",
    "Open with what the animal actually does, then reveal the body part or reason behind it, and end with one short question.",
    "Open with a one-line claim most people would doubt, prove it in two short paragraphs, and end with a small joke.",
    "Tell it as a quick chain of what the animal does next, every paragraph a different length, and end with no moral at all.",
    "Open in the middle of the action (a chase, a dive, a leap), jump back to explain, and finish on a surprising detail.",
]


def write_post(title, source, recent):
    system = WRITER_SYSTEM
    user = (
        f"SOURCE ARTICLE TITLE: {title}\n\nSOURCE TEXT:\n{source}\n\n"
        f"Do NOT repeat or closely resemble any of these recent animals or facts: {json.dumps(recent)}\n"
        f"VOICE for the caption: {random.choice(VOICES)}.\nSHAPE for the caption: {random.choice(SHAPES)}\n"
        "Write the post using ONLY the SOURCE TEXT. Return the JSON object now."
    )
    return chat_json(system, user, WRITER_MODELS, 0.7, tries=1, max_tokens=5000)


def check_faithful(post, source):
    draft = {"headline_lines": post["headline_lines"], "caption": post["caption"]}
    user = f"SOURCE TEXT:\n{source}\n\nDRAFT:\n{json.dumps(draft, ensure_ascii=False)}"
    if WEB_CHECK:
        return chat_json(CHECKER_WEB_SYSTEM, user, CHECKER_MODELS, 0.1, max_tokens=1500, plugins=[{"id": "web", "max_results": 3}])
    return chat_json(CHECKER_SYSTEM, user, CHECKER_MODELS, 0.1, max_tokens=1500)


def ensure_highlight(lines):
    """If the model forgot [brackets], auto-highlight numbers, prices and years in red."""
    if any("[" in l for l in lines):
        return lines
    pat = re.compile(r"(\d[\d,\.]*\s?(?:KM/H|MPH|KG|KILOGRAMS?|TONNES?|TONS?|KM|KILOMETRES?|KILOMETERS?|METRES?|METERS?|CM|CENTIMETRES?|MM|MILES?|FEET|FT|%|YEARS?|HOURS?|MINUTES?|DAYS?|WEEKS?|MONTHS?|EGGS?|HEARTS?|LEGS?|TEETH)?)", re.I)
    out = [pat.sub(lambda m: f"[{m.group(1).strip()}]" + (" " if m.group(1).endswith(" ") else ""), l) for l in lines]
    if any("[" in l for l in out):
        return out
    words = re.findall(r"[A-Za-z']{4,}", out[-1])   # no number in the hook: colour its longest word instead
    if words:
        w = max(words, key=len)
        out[-1] = out[-1].replace(w, f"[{w}]", 1)
    return out


def _runs(line):
    """Split a line into (text, is_red) runs. Brackets only switch the colour on and off and never survive."""
    runs, red, buf = [], False, ""
    for ch in line:
        if ch == "[":
            if buf:
                runs.append((buf, red))
                buf = ""
            red = True
        elif ch == "]":
            if buf:
                runs.append((buf, red))
                buf = ""
            red = False
        else:
            buf += ch
    if buf:
        runs.append((buf, red))
    return runs


def fix_brackets(line):
    """Repair common model mistakes: $[100] -> [$100], [1930,] -> [1930],, unbalanced brackets, parentheses."""
    line = re.sub(r"([$\u00a3\u20ac])\[", r"[\1", line)
    line = re.sub(r"([,.;:!?]+)\]", r"]\1", line)
    if line.count("[") != line.count("]"):
        line = line.replace("[", "").replace("]", "")
    return line.replace("(", "").replace(")", "")


def limit_red(lines, max_runs=2):
    """Keep at most `max_runs` red groups, so the headline never turns into a red wall."""
    out, runs = [], 0
    for line in lines:
        res = ""
        for text, red in _runs(line):
            if red:
                runs += 1
                res += f"[{text}]" if runs <= max_runs else text
            else:
                res += text
        out.append(res)
    return out


def rewrap_headline(lines, width=30):
    """Re-wrap headline words into lines of at most `width` characters, keeping the red words red."""
    chars = []
    for line in lines:
        for text, red in _runs(line):
            chars.extend((c, red) for c in text)
        chars.append((" ", False))
    words, cur = [], []
    for ch, red in chars:
        if ch.isspace():
            if cur:
                words.append(cur)
                cur = []
        else:
            cur.append((ch, red))
    if cur:
        words.append(cur)

    def plain(ws):
        return " ".join("".join(c for c, _ in w) for w in ws)

    out, cur_line = [], []
    for w in words:
        if cur_line and len(plain(cur_line + [w])) > width:
            out.append(cur_line)
            cur_line = []
        cur_line.append(w)
    if cur_line:
        out.append(cur_line)

    rendered = []
    for ws in out:
        seq = []
        for i, w in enumerate(ws):
            if i:
                seq.append((" ", ws[i - 1][-1][1] and w[0][1]))
            seq.extend(w)
        text, i = "", 0
        while i < len(seq):
            j = i
            while j < len(seq) and seq[j][1] == seq[i][1]:
                j += 1
            chunk = "".join(c for c, _ in seq[i:j])
            text += f"[{chunk}]" if seq[i][1] else chunk
            i = j
        rendered.append(text)
    return rendered


SPELLED_NUM = re.compile(
    r"\b(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|hundred|thousand|million|billion)\b", re.I)
SPELLED_POINT = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten) point (?:zero|one|two|three|four|five|six|seven|eight|nine)\b", re.I)


def has_spelled_numbers(text):
    """Numbers written as words slip past the digit check, so a caption full of them is not allowed."""
    return bool(SPELLED_POINT.search(text)) or len(SPELLED_NUM.findall(text)) >= 2


def ensure_paragraphs(cap):
    """A wall of text becomes 3 to 4 paragraphs of uneven length."""
    cap = cap.strip()
    if "\n\n" in cap:
        return cap
    cap = cap.replace("\n", " ")
    sents = re.split(r"(?<=[.!?])\s+", cap)
    n = len(sents)
    if n < 4:
        return cap
    cuts = sorted({max(1, round(n * f)) for f in (0.2, 0.5, 0.8)})
    cuts = [c for c in cuts if c < n]
    parts, last = [], 0
    for c in cuts + [n]:
        parts.append(" ".join(sents[last:c]))
        last = c
    return "\n\n".join(x for x in parts if x)


def validate_post(post, level=0):
    """level 0 = normal, 1 = relaxed, 2 = minimal. Formatting is always repaired; only level 0 is picky."""
    lines = post.get("headline_lines")
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list) or not [l for l in lines if str(l).strip()]:
        if level == 0:
            return "headline missing"
        lines = [str(post.get("topic") or "WILD ANIMAL FACTS").upper()]
    lines = [fix_brackets(str(l)).strip() for l in lines if str(l).strip()]
    if len(lines) == 1 or len(lines) > 4 or any(len(re.sub(r"[\[\]]", "", l)) > 32 for l in lines):
        lines = rewrap_headline(lines)
    lines = limit_red(lines)[:4]
    post["headline_lines"] = lines
    words = len(" ".join(lines).split())
    if level == 0:
        if not 6 <= words <= 15:
            return f"headline word count {words}"
    cwords = len(str(post.get("caption", "")).split())
    if level == 0 and not 150 <= cwords <= 450:
        return f"caption word count {cwords}"
    if level == 1 and cwords < 110:
        return f"caption word count {cwords}"
    if level == 2 and cwords < 60:
        return f"caption word count {cwords}"
    cap = re.sub(r"\s?\[\d{1,2}\]", "", post["caption"])
    cap = re.sub(r"\s*[\u2014\u2013]\s*", ", ", cap)  # no em/en dashes (AI tell)
    cap = re.sub(r"[*_#`]+", "", cap).strip()
    cap = ensure_paragraphs(cap)
    post["caption"] = cap
    if level < 2 and has_spelled_numbers(cap):
        return "numbers spelled out in words (must be digits)"
    post["headline_lines"] = ensure_highlight(lines)
    return None


def scrub_numbers(post, source, title):
    """Never publish an amount or year the source does not contain: drop such sentences, or use a neutral headline."""
    known = numbers_in(source, False)

    def unsupported(text):
        return numbers_in(text, True) - known

    if unsupported(" ".join(post["headline_lines"])):
        log("headline had numbers that are not in the source; using a neutral headline")
        post["headline_lines"] = ensure_highlight(rewrap_headline(["THE STORY OF", f"[{title.upper()}]"]))
    kept, dropped = [], 0
    for para in re.split(r"\n+", post["caption"]):
        sents = re.split(r"(?<=[.?!])\s+", para.strip())
        good = [x for x in sents if x and not unsupported(x)]
        dropped += len(sents) - len(good)
        if good:
            kept.append(" ".join(good))
    if dropped:
        log(f"dropped {dropped} caption sentence(s) with numbers that are not in the source")
    post["caption"] = "\n\n".join(kept) if kept else post["caption"]


def add_hashtags(post):
    tags = ["#WildloreFacts"]
    for t in post.get("hashtags") or []:
        t = "#" + re.sub(r"[^A-Za-z0-9]", "", str(t))
        if len(t) > 3 and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    tail = " ".join(tags[:3])
    if SHOW_SOURCE and post.get("source_title"):
        tail = f'Source: Wikipedia, "{post["source_title"]}"\n' + tail
    post["caption"] = post["caption"].rstrip() + "\n\n" + tail


RUN_BUDGET_MIN = float(os.environ.get("RUN_BUDGET_MINUTES", "20"))


HEDGE_WORDS = re.compile(r"\b(reportedly|according to|accounts|about|roughly|estimated|around|some sources|records suggest)\b", re.I)


def _words(text):
    return re.sub(r"[^a-z0-9$%. ]+", " ", text.lower().replace("\u2019", "'")).split()


def check_originality(post, source):
    """Own words, story shape, nothing modern, cautious wording for percentages. Returns a problem text or None."""
    cap = post["caption"]
    paras = [x.strip() for x in re.split(r"\n+", cap) if x.strip()]
    if len(paras) < 3:
        return f"caption has {len(paras)} paragraphs, needs 3 or more"
    post["caption"] = "\n\n".join(paras)
    first = re.split(r"(?<=[.?!])\s", paras[0], maxsplit=1)[0]
    if len(first) > 140:
        return f"first sentence is {len(first)} characters, limit 140"
    src = _words(source)
    grams = {" ".join(src[i : i + 6]) for i in range(max(0, len(src) - 5))}
    cw = _words(cap)
    for i in range(max(0, len(cw) - 5)):
        g = " ".join(cw[i : i + 6])
        if g in grams:
            return f"caption copies the source: '{g}'"
    head = " ".join(post["headline_lines"])
    if ("%" in head or re.search(r"\b\d+\s*(?:to|-)\s*\d+\b", head)) and not HEDGE_WORDS.search(cap):
        return "percentage or range in the headline, but the caption has no cautious wording"
    return None


AI_MARKERS = re.compile(
    r"\b(delve[sd]?|tapestry|testament|pivotal|crucial|moreover|furthermore|additionally|notably|ultimately|underscor\w+|paved the way|"
    r"ignit\w+|spark(?:ed|s|ing) (?:a|an|the)|it'?s worth noting|in conclusion|then versus now|landscape of|realm of|"
    r"play(?:s|ed) a (?:crucial|key|vital|significant) role|a turning point|serves as|stands as|here'?s the thing)\b", re.I)


def _sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if x.strip()]


def ai_likeness(text):
    """Rough local meter (0 = very human, 100+ = very machine-like). Low variety, tidy rhythm and stock phrases raise it."""
    sents = _sentences(text)
    if len(sents) < 4:
        return 50.0
    lens = [len(x.split()) for x in sents]
    mean = sum(lens) / len(lens)
    sd = (sum((n - mean) ** 2 for n in lens) / len(lens)) ** 0.5
    cv = sd / max(mean, 1)
    short = sum(1 for n in lens if n <= 6) / len(lens)
    long_ = sum(1 for n in lens if n >= 25) / len(lens)
    starts = {}
    for x in sents:
        w = x.split()[0].lower()
        starts[w] = starts.get(w, 0) + 1
    score = max(0.0, 0.65 - cv) * 100
    score += 15 if short < 0.10 else 0
    score += 10 if long_ < 0.05 else 0
    score += 10 if max(starts.values()) / len(sents) > 0.3 else 0
    score += 8 * len(AI_MARKERS.findall(text))
    score += 20 if re.match(r"\s*(picture|imagine)\b", text, re.I) else 0
    score += 8 * max(0, text.count("?") - 1)
    return score


DEAI = [
    (r"\bIt'?s worth noting that\s*", ""), (r"\bIn conclusion,\s*", ""), (r"\b(?:Moreover|Furthermore|Additionally),\s*", "Also, "),
    (r"\bNotably,\s*", ""), (r"\bUltimately,\s*", ""), (r"\bdelve into\b", "dig into"), (r"\btestament to\b", "proof of"),
    (r"\btapestry\b", "mix"), (r"\blandscape of\b", "world of"), (r"\brealm of\b", "world of"), (r"\bpivotal\b", "big"),
    (r"\bcrucial\b", "big"), (r"\bpaved the way for\b", "led to"), (r"\bunderscores?\b", "shows"),
]


def deai(text):
    """Deterministic clean-up: no 'Picture...' opener, no template lines, one question at most (the last), no stock phrases."""
    text = re.sub(r"^\s*(?:Picture|Imagine)(?: this)?[:,]?\s+", "", text)
    text = text[:1].upper() + text[1:] if text else text
    for pat, rep_ in DEAI:
        text = re.sub(pat, rep_, text, flags=re.I)
    paras = [x.strip() for x in re.split(r"\n+", text) if x.strip()]
    out = []
    for i, para in enumerate(paras):
        sents = _sentences(para)
        keep = []
        for j, x in enumerate(sents):
            last = i == len(paras) - 1 and j == len(sents) - 1
            if re.match(r"(Then versus now|Then:)", x, re.I):
                continue
            if x.endswith("?") and not last:
                continue  # rhetorical questions in the middle read like a template
            keep.append(x)
        if keep:
            out.append(" ".join(keep))
    return "\n\n".join(out) if out else text


HUMANIZE_SYSTEM = """You are an editor who makes machine-sounding text read like a real person wrote it, without changing a single fact.
You get a CAPTION. Rewrite it so that it:
- keeps every fact, number, year and name exactly; adds no new fact, number or name;
- uses uneven sentence lengths (some under 6 words, some over 25), fragments, contractions and plain words;
- sounds like one person with an opinion talking to a friend, not like a report or an encyclopedia;
- has no summary, no moral and no lesson at the end; at most ONE question in the whole text, and only as the very last sentence;
- never uses: Picture, Imagine, delve, tapestry, testament, pivotal, crucial, moreover, furthermore, additionally, notably, ultimately, underscore, "paved the way", "ignited", "sparked a", "It's worth noting", "Then versus now";
- has 3 to 6 paragraphs of different lengths separated by a blank line (write \\n\\n inside the JSON string);
- has a first sentence under 120 characters that holds the surprising fact;
- stays between {lo} and {hi} words; no em dashes, no emojis, no hashtags.
Return ONLY JSON: {{"caption": "..."}}"""


def humanize_caption(post, source):
    """Free and never blocking: if anything goes wrong the original caption (cleaned) is used."""
    original = deai(post["caption"])
    best, best_score = original, ai_likeness(original)
    start_score = best_score
    allowed = numbers_in(source, False)
    try:
        if best_score >= 18:
            words = len(original.split())
            system = HUMANIZE_SYSTEM.format(lo=max(110, int(words * 0.8)), hi=int(words * 1.15) + 20)
            models = list(CHECKER_MODELS) or list(WRITER_MODELS)
            for k in range(2):
                order = models[k % max(len(models), 1):] + models[: k % max(len(models), 1)]
                try:
                    out = chat_json(system, f"CAPTION:\n{best if k else original}", order, 1.0, tries=1, max_tokens=2500)
                except Exception as e:  # noqa: BLE001
                    log("human-voice editor unavailable:", str(e)[:100])
                    break
                cand = deai(str(out.get("caption", "")).strip())
                cand = re.sub(r"\s*[\u2014\u2013]\s*", ", ", cand)
                if len(cand.split()) < max(70, int(0.6 * words)) or numbers_in(cand, True) - allowed:
                    log("human-voice rewrite dropped (too short or it changed the numbers)")
                    continue
                sc = ai_likeness(cand)
                if sc < best_score:
                    best, best_score = cand, sc
                if best_score < 18:
                    break
    except Exception as e:  # noqa: BLE001
        log("human-voice step skipped:", e)
    log(f"AI-likeness meter: {start_score:.0f} -> {best_score:.0f} (lower is more human)")
    return best


def emergency_post(title, source):
    """Last resort without any AI: a plain, attributed post from the article's own opening lines."""
    flat = re.sub(r"\s+", " ", re.sub(r"=+[^=]+=+", " ", source[:4000]))
    sents = [x for x in re.split(r"(?<=[.!?])\s+", flat) if 40 < len(x) < 320][:4]
    caption = "\n\n".join(sents) + f'\n\nWhat part of this surprises you most?\n\nSource: Wikipedia, "{title}"\n#WildloreFacts #Animals'
    return {
        "topic": title, "source_title": title, "habitat": "",
        "headline_lines": ensure_highlight(rewrap_headline(["THE STORY OF", f"[{title.upper()}]"])),
        "subhook": "A WILD ANIMAL STORY", "caption": caption,
        "image_prompt_a": f"{title}, the animal in its natural habitat, professional wildlife photograph, natural light",
        "image_prompt_b": f"{title}, close-up portrait of the animal, shallow depth of field, soft natural light",
    }


EXPAND_SYSTEM = """You extend short social-media captions about animals.
Rewrite the CAPTION so it is 170-230 words. Use ONLY facts that the SOURCE TEXT states: add 3 to 5 more concrete details (other numbers, names, behaviours, places) from it.
Keep every number and unit exactly as in the SOURCE TEXT and the CAPTION. Write numbers as digits (25.2 metres, 2017), never spelled out in words. Never convert a unit and never add "nearly", "almost" or "roughly" to a number.
Keep the voice: plain American English, one sharp nature nerd telling a friend, short and long sentences mixed, contractions, no AI cliches (delve, tapestry, testament, pivotal, crucial, moreover, furthermore, "a turning point"), no em dashes, no emojis, no hashtags.
4 to 6 paragraphs of uneven length, separated by a blank line (write \\n\\n inside the JSON string). End with ONE blunt line, a small joke or one short question.
Return ONLY JSON: {"caption": "..."}"""


def expand_caption(post, source, minimum):
    """Free models often write 70 to 100 words although 150 were asked. One extra call usually fixes it."""
    cap = str(post.get("caption", ""))
    words = len(cap.split())
    if words < 40 or words >= minimum:
        return False
    try:
        out = chat_json(EXPAND_SYSTEM, f"SOURCE TEXT:\n{source[:6000]}\n\nCAPTION:\n{cap}",
                        list(WRITER_MODELS), 0.8, tries=1, max_tokens=2500)
    except Exception as e:  # noqa: BLE001
        log("caption expander unavailable:", str(e)[:100])
        return False
    new = str(out.get("caption", "")).strip()
    if len(new.split()) < minimum or numbers_in(new, True) - numbers_in(source, False) or has_spelled_numbers(new):
        log("caption expansion dropped (still short, new numbers, or numbers spelled out)")
        return False
    log(f"caption expanded: {words} -> {len(new.split())} words")
    post["caption"] = new
    return True


def build_post(recent, used):
    """Tries hard, then softer, then softest, then a plain post: a run always ends with something worth publishing."""
    deadline = time.time() + RUN_BUDGET_MIN * 60
    for attempt in range(1, 13):
        if time.time() > deadline:
            log(f"time budget of {RUN_BUDGET_MIN:.0f} minutes used up")
            break
        level = 0 if attempt <= 4 else (1 if attempt <= 8 else 2)
        try:
            title, source = pick_source(used, level)
            used.add(title)
            log(f"attempt {attempt} (level {level}): source article: {title}")
            post = write_post(title, source, recent)
            if post.get("skip"):
                log("writer skipped:", post.get("reason"))
                continue
            if not post.get("caption"):
                raise ValueError("missing caption")
            post.setdefault("topic", title)
            if not post.get("topic"):
                post["topic"] = title
            problem = validate_post(post, level)
            if problem and problem.startswith("caption word count"):
                if expand_caption(post, source, {0: 150, 1: 110}.get(level, 60)):
                    problem = validate_post(post, level)
            if problem:
                log("rejected:", problem)
                continue
            note = check_originality(post, source)
            if note:
                log("note (not blocking):", note)
            if level == 0:
                hard = [x for x in verify_grounding(post, source)
                        if "numbers not in source" in x.lower() or ("quote not found in source" in x.lower() and len(x) > 60)]
                if hard:
                    log("grounding failed:", hard)
                    continue
                verdict = check_faithful(post, source)
                if not verdict.get("faithful"):
                    bad = [u for u in (verdict.get("unsupported") or [])
                           if any(k in u.lower() for k in ("not in the source", "unsupported", "wrong", "missing", "not supported"))]
                    if bad:
                        log("checker: unsupported claims:", bad)
                        continue
                try:
                    if float(verdict.get("hook_score", 0)) < 5:
                        log("rejected: weak hook", verdict.get("hook_score"))
                        continue
                except (TypeError, ValueError):
                    pass
            else:
                scrub_numbers(post, source, title)
            post["caption"] = humanize_caption(post, source)
            post["source_title"] = title
            post["confidence"] = f"level {level}"
            post["sources"] = [f"Wikipedia: {title}"]
            post["issues"] = []
            add_hashtags(post)
            return post
        except Exception as e:  # noqa: BLE001
            log("attempt failed:", e)
    if _LAST_SOURCE:
        title, source = _LAST_SOURCE
        log(f"EMERGENCY POST from the article {title!r}: the AI writer did not deliver")
        post = emergency_post(title, source)
        return post
    raise RuntimeError("no article could be fetched (Wikipedia unreachable)")


# ----------------------------------------------------------------- image
NEGATIVE_SD = (
    "deformed, bad anatomy, extra legs, extra heads, extra eyes, missing limbs, fused bodies, melted face, "
    "blurry, lowres, text, watermark, logo, cartoon, anime, painting, 3d render, oversaturated, duplicate, cropped, "
    "human, person, hands, cage, crowd"
)
FACELESS = os.environ.get("FACELESS", "0") == "1"
IMAGE_QA = os.environ.get("IMAGE_QA", "1") == "1"
GUIDANCE = os.environ.get("GUIDANCE", "2")   # LCM models want about 1-2; "0" = do not send
QA_MODELS = _models("IMAGE_QA_MODEL", "")      # empty = automatic: free vision models
QA_PROMPT = (
    "You are a strict quality reviewer for AI-generated wildlife photographs. Reject the image if you see ANY of: "
    "a distorted, melted or asymmetric animal face; wrong number of legs, wings, eyes or tails for the species; "
    "two heads or merged, duplicated animals; any human figure or visible text; impossible or fused body parts; a cartoon, anime or painted look; colours or body shape that do not match the real species (for example a brightly coloured or cute-faced deep-sea fish). "
    'Return ONLY JSON: {"ok": true or false, "score": 1-10, "problems": ["..."]}. Score 8 or more only if it looks like a clean real photograph.'
)
_QA_OFF = [False]


def _small_model(model):
    return any(t in model.lower() for t in ("dreamshaper", "lcm", "sd15", "stable-diffusion"))


def _gen_size(model):
    if _small_model(model):  # SD-1.5 models break above ~768 px; the server may return a slightly smaller size
        try:
            w, h = os.environ.get("GEN_SIZE", "512x640").lower().split("x")
            return int(w), int(h)
        except ValueError:
            return 512, 640
    return GEN_W, GEN_H


def _shorten(text, n):
    return " ".join(str(text or "").replace("\n", " ").split()[:n]).rstrip(".,;")


def _era_mode(post):
    """Animals are always shot in natural colour."""
    return "color"


def build_image_prompt(post, variant=0, model=None):
    """The writer's scene for this story (idea A, or idea B on every second candidate), shaped for the model that will draw it."""
    model = model or IMAGE_MODELS[0]
    text = (post.get("image_prompt_b") if variant == 1 and post.get("image_prompt_b") else post.get("image_prompt_a")) or ""
    if not text:  # older field names
        text = f"{post.get('image_subject') or post.get('image_prompt') or ''}, {post.get('image_scene') or ''}"
    era = _shorten(post.get("habitat") or "", 4)
    if _small_model(model):  # SD-1.5 reads only about 60 words: key subject first
        style = "wildlife photograph, natural light, sharp focus, detailed fur and eyes, true colours"
        return f"{_shorten(text, 38)}, {era}, {style}"[:520].lstrip(", ")
    return (f"{text.strip().rstrip('.')}. {era}. {STYLE_SUFFIX}. Vertical 4:5 frame: the main subject sits in the upper two thirds "
            "and the lowest part of the frame is a darker foreground such as grass, rock, water or soil. "
            "True-to-life anatomy and natural colours of the real species, documentary wildlife photo, not a cartoon, no extra fins, legs or eyes. "
            "Sharp focus, natural look, fine film grain, photorealistic, high detail. No watermarks.")[:1500]


_BAD_IMG = set()   # image models that refused us in this run (no credit, unknown model); skipped for the rest of the run


_CF_BAD = [False]   # set when Cloudflare refuses us (wrong token, no allowance left); skipped for the rest of the run


def _cf_candidate(post, variant):
    """One picture from Cloudflare Workers AI. Raises RuntimeError on any problem so the caller can fall back."""
    prompt = build_image_prompt(post, variant, "flux")[:2000]
    body = {"prompt": prompt, "steps": CF_STEPS}
    if CF_SIZE:
        try:
            w, h = CF_SIZE.lower().split("x")
            body["width"], body["height"] = int(w), int(h)
        except ValueError:
            log("CF_SIZE ignored (use the form 1024x1280):", CF_SIZE)
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    last = None
    for i in range(2):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"}, json=body, timeout=120)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200:
                raw = None
                if ctype.startswith("image/"):
                    raw = r.content
                else:
                    data = r.json()
                    b64 = (data.get("result") or {}).get("image") or data.get("image")
                    if b64:
                        raw = base64.b64decode(b64)
                if raw:
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    _NEURONS["image"] += math.ceil(img.size[0] / 512) * math.ceil(img.size[1] / 512) * 4.8 + CF_STEPS * 9.6   # flux-1-schnell price estimate
                    log(f"image candidate ok (Cloudflare {CF_MODEL}, {img.size[0]}x{img.size[1]}, try {i + 1})")
                    return ImageOps.fit(img, (W, H), method=Image.LANCZOS, centering=(0.5, 0.4)), False
            last = f"HTTP {r.status_code} {r.text[:200]}"
            low = r.text.lower()
            if r.status_code in (401, 403):
                _CF_BAD[0] = True
                raise RuntimeError(f"Cloudflare refused the token ({last[:120]}). Check CF_API_TOKEN has the Workers AI permission and CF_ACCOUNT_ID is right")
            if "neuron" in low or "allocation" in low or "quota" in low or "daily" in low:
                _CF_BAD[0] = True
                raise RuntimeError(f"Cloudflare free allowance used up for today ({last[:120]})")
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
        log(f"cloudflare image call failed ({i + 1}/2): {str(last)[:160]}")
        time.sleep(4 * (i + 1))
    raise RuntimeError(f"cloudflare image failed: {str(last)[:160]}")


def _fetch_candidate(post, variant, tag):
    if CF_ACCOUNT_ID and CF_API_TOKEN and not _CF_BAD[0]:
        try:
            return _cf_candidate(post, variant)
        except Exception as e:  # noqa: BLE001
            log(f"Cloudflare image failed: {str(e)[:220]}; using Pollinations instead")
    if not KEY:
        raise RuntimeError("no image service available: Cloudflare failed and POLLINATIONS_API_KEY is not set")
    last = None
    for i in range(3):
        models = [m for m in IMAGE_MODELS if m not in _BAD_IMG] or IMAGE_MODELS
        model = models[min(i, len(models) - 1)]
        prompt = build_image_prompt(post, variant, model)
        gw, gh = _gen_size(model)
        neg = NEGATIVE_SD if _small_model(model) else NEGATIVE_PROMPT
        seed = random.randint(1, 10**8)
        params = f"model={quote(model, safe='')}&width={gw}&height={gh}&seed={seed}&nologo=true"
        if i == 0 and _small_model(model) and GUIDANCE not in ("", "0"):
            params += f"&guidance_scale={GUIDANCE}"  # first try only; later tries drop it in case the server rejects it
        if i < 2:  # last try drops the negative prompt too
            params += f"&negative_prompt={quote(neg)}"
        try:
            r = requests.get(f"{API}/image/{quote(prompt)}?{params}", headers={"Authorization": f"Bearer {KEY}"}, timeout=240)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                log(f"image candidate ok (model {model}, {img.size[0]}x{img.size[1]}, try {i + 1})")
                return ImageOps.fit(img, (W, H), method=Image.LANCZOS, centering=(0.5, 0.4)), _small_model(model)
            last = f"HTTP {r.status_code} {r.text[:160]}"
            if r.status_code in (400, 401, 402, 403, 404) or "credit" in r.text.lower() or "pollen" in r.text.lower():
                _BAD_IMG.add(model)
                log(f"image model {model} refused ({last[:100]}); using the next one")
        except Exception as e:  # noqa: BLE001
            last = e
        log(f"image call failed ({i + 1}/3, model {model}): {str(last)[:160]}")
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"image generation failed: {last}")


VINTAGE_GRADE = os.environ.get("VINTAGE_GRADE", "0") == "1"   # off for animals: natural colour


def _vintage(img, mode):
    """Sepia (or faded colour) toning, vignette, grain and a few dust specks: reads as an old archive photo and hides softness."""
    w, h = img.size
    if mode == "mono":
        g = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        g = ImageEnhance.Contrast(g).enhance(1.12)
        out = ImageOps.colorize(g, black=(18, 11, 6), white=(244, 230, 201), mid=(138, 108, 76))
    else:
        out = ImageEnhance.Color(img).enhance(0.78)
        out = Image.blend(out, ImageChops.multiply(out, Image.new("RGB", (w, h), (255, 232, 200))), 0.35)
    edge = Image.radial_gradient("L").resize((w, h)).point(lambda v: int(v * 0.55))  # dark corners
    out = Image.composite(ImageEnhance.Brightness(out).enhance(0.45), out, edge)
    out = Image.blend(out, Image.effect_noise((w, h), 34).convert("RGB"), 0.06)  # film grain
    d = ImageDraw.Draw(out)
    for _ in range(40):  # dust specks
        x, y, r = random.randint(0, w - 1), random.randint(0, h - 1), random.choice((1, 1, 2))
        d.ellipse([x, y, x + r, y + r], fill=random.choice(((236, 226, 204), (30, 22, 14))))
    return out


def _finish(img, upscaled, mode="mono"):
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.4 if upscaled else 1.2, percent=80 if upscaled else 65, threshold=3))
    if upscaled and VINTAGE_GRADE:
        return _vintage(img, mode)
    img = ImageEnhance.Color(img).enhance(1.05)
    if upscaled:
        img = Image.blend(img, Image.effect_noise((W, H), 30).convert("RGB"), 0.05)
    return img


_QA_JUDGE = []


def vision_check(img, story=""):
    """Ask free vision models whether the image is distorted. Retries once if they are all busy. None = unavailable."""
    if not QA_MODELS:
        return None
    try:
        small = img.copy()
        small.thumbnail((640, 800))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": QA_PROMPT + (
                f' The picture must illustrate this story: "{story}". Add "matches_story": true only if the main subject of the picture '
                'clearly belongs to that story (a story about an owl needs an owl; a different animal or no animal is false), otherwise false.'
                if story else "")},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}]
        for round_ in range(2):
            now = time.time()
            order = list(QA_MODELS)
            if _QA_JUDGE and _QA_JUDGE[0] in order:  # one judge for all candidates, so the scores are comparable
                order.remove(_QA_JUDGE[0])
                order.insert(0, _QA_JUDGE[0])
            if round_ == 0:  # models that were busy a moment ago go last, they usually still are
                order.sort(key=lambda m: _BUSY.get(m, 0) > now)
            for model in order:
                if round_ == 0 and _BUSY.get(model, 0) > now and any(_BUSY.get(x, 0) <= now for x in order):
                    continue
                try:
                    result = extract_json(or_call(messages, model, temperature=0, max_tokens=400))
                    _QA_JUDGE[:] = [model]
                    return result
                except Exception as e:  # noqa: BLE001
                    log(f"[image QA {model}] unavailable: {str(e)[:120]}")
            if round_ == 0:
                time.sleep(8)
    except Exception as e:  # noqa: BLE001
        log("image QA error:", e)
    return None


def generate_image(post):
    """A few candidates, alternating the writer's two ideas. First one that passes QA wins, else the best scored."""
    best, best_score, best_up = None, -1.0, False
    qa_failures = 0
    image_failures = 0
    mode = _era_mode(post)
    story = re.sub(r"[\[\]]", "", " ".join(post.get("headline_lines") or []))
    for n in range(max(1, int(os.environ.get("IMAGE_CANDIDATES", "3") or 3))):
        try:
            img, up = _fetch_candidate(post, n % 2, n)
        except RuntimeError as e:
            log(e)
            image_failures += 1
            if image_failures >= 2 and best is None:
                break  # the image service is down: use the text card instead of waiting longer
            continue
        if not IMAGE_QA:
            return _finish(img, up, mode)
        verdict = None if qa_failures >= 2 else vision_check(img, story)
        if verdict is None:
            log(f"image QA unavailable for candidate #{n + 1}: using it unrated (no point generating more)")
            return _finish(img, up, mode)
        else:
            on_topic = verdict.get("matches_story") is not False
            ok = bool(verdict.get("ok")) and on_topic
            try:
                score = float(verdict.get("score", 8 if ok else 3))
            except (TypeError, ValueError):
                score = 8.0 if ok else 3.0
            if not on_topic:
                score = min(score, 4.0)
            log(f"image QA #{n + 1}: ok={ok} on_topic={on_topic} score={score} problems={verdict.get('problems')}")
            if ok and score >= 6:
                return _finish(img, up, mode)
        if score > best_score:
            best, best_score, best_up = img, score, up
    if best is not None:
        log(f"no image passed QA; using the best candidate (score {best_score})")
        return _finish(best, best_up, mode)
    raise RuntimeError("image generation failed (no candidate)")


def fallback_background():
    """If no picture can be made at all, the post still goes out on a dark forest-green card."""
    base = Image.new("RGB", (W, H), (20, 48, 34))
    d = ImageDraw.Draw(base)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(40 - 26 * t), int(92 - 58 * t), int(62 - 40 * t)))
    return base


FONT_DIR = HERE / "fonts"

# One look per style: font, text colour, keyword colour, shadow kind (hard 3D drop / glow / outline), bottom tint,
# alignment and subhook design. Every post gets a different one (never one of the last 4 used).
STYLES = [
    {"name": "classic", "font": "Anton-Regular.ttf",        "text": (244, 244, 240), "accent": (214, 70, 62),   "shadow": ("hard", (0, 0, 0)),       "tint": (0, 0, 0),    "align": "center", "sub": "line", "lh": 1.08, "k": 1.00},
    {"name": "gold",    "font": "AbrilFatface-Regular.ttf", "text": (250, 240, 214), "accent": (232, 190, 105), "shadow": ("hard", (62, 30, 6)),     "tint": (22, 10, 2),  "align": "center", "sub": "line", "lh": 1.12, "k": 0.92},
    {"name": "ice",     "font": "BebasNeue-Regular.ttf",    "text": (244, 246, 248), "accent": (134, 196, 226), "shadow": ("glow", (28, 62, 120)),   "tint": (2, 8, 26),   "align": "left",   "sub": "chip", "lh": 1.02, "k": 1.14},
    {"name": "sun",     "font": "ArchivoBlack-Regular.ttf", "text": (244, 224, 140), "accent": (244, 244, 240), "shadow": ("hard", (0, 0, 0)),       "tint": (0, 0, 0),    "align": "center", "sub": "line", "lh": 1.10, "k": 0.78},
    {"name": "pink",    "font": "Poppins-ExtraBold.ttf",    "text": (244, 244, 240), "accent": (232, 118, 156), "shadow": ("hard", (74, 16, 92)),    "tint": (14, 4, 22),  "align": "left",   "sub": "chip", "lh": 1.12, "k": 0.82},
    {"name": "lime",    "font": "Staatliches-Regular.ttf",  "text": (244, 244, 240), "accent": (190, 224, 120), "shadow": ("outline", (6, 40, 20)),  "tint": (2, 18, 8),   "align": "center", "sub": "line", "lh": 1.04, "k": 1.14},
    {"name": "ember",   "font": "RussoOne-Regular.ttf",     "text": (244, 244, 240), "accent": (236, 156, 84),  "shadow": ("hard", (96, 14, 10)),    "tint": (20, 4, 2),   "align": "left",   "sub": "line", "lh": 1.10, "k": 0.86},
    {"name": "lagoon",  "font": "Righteous-Regular.ttf",    "text": (232, 242, 248), "accent": (122, 208, 188), "shadow": ("glow", (10, 90, 90)),    "tint": (2, 16, 20),  "align": "center", "sub": "chip", "lh": 1.10, "k": 0.92},
    {"name": "coral",   "font": "PassionOne-Bold.ttf",      "text": (250, 244, 236), "accent": (238, 128, 108), "shadow": ("hard", (90, 20, 30)),    "tint": (18, 6, 8),   "align": "center", "sub": "line", "lh": 1.04, "k": 1.14},
    {"name": "slab",    "font": "AlfaSlabOne-Regular.ttf",  "text": (250, 242, 222), "accent": (232, 182, 88),  "shadow": ("outline", (40, 22, 8)),  "tint": (14, 8, 2),   "align": "left",   "sub": "line", "lh": 1.10, "k": 0.82},
]
RECENT_STYLES = []   # style names of the latest posts (filled from history.json)
_STYLE_USED = [""]


def pick_style():
    fresh = [s for s in STYLES if s["name"] not in RECENT_STYLES[-4:]] or STYLES
    return random.choice(fresh)


def load_font(size, name=None):
    path = FONT_DIR / (name or "Anton-Regular.ttf")
    if not path.exists():
        path = FONT_PATH if FONT_PATH.exists() else FALLBACK_FONT
    return ImageFont.truetype(str(path), size)


def _ascii(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")


def parse_segments(line, st):
    return [(t, st["accent"] if red else st["text"]) for t, red in _runs(line) if t]


def _smooth(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _spaced(draw, x0, y, text, font, fill, spacing=3, shadow=True):
    x = x0
    for c in text:
        if shadow:
            draw.text((x + 2, y + 3), c, font=font, fill=(0, 0, 0))
        draw.text((x, y), c, font=font, fill=fill)
        x += font.getlength(c) + spacing
    return x


def _spaced_width(text, font, spacing=3):
    return sum(font.getlength(c) + spacing for c in text) - spacing


def _headline_layers(img, items, font, st):
    """glow / 3D drop shadow / outline behind the text, then the text itself."""
    kind, scol = st["shadow"]
    size = font.size
    if kind == "glow":
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for x, y, t, _ in items:
            md.text((x, y), t, font=font, fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(max(16, size // 5))).point(lambda v: min(255, int(v * 0.8)))
        img.paste(Image.new("RGB", (W, H), scol), (0, 0), mask)
    d = ImageDraw.Draw(img)
    for x, y, t, _ in items:
        d.text((x + 2, y + 4), t, font=font, fill=(0, 0, 0))
    if kind == "hard":
        for i in range(1, max(4, size // 18) + 1):
            for x, y, t, _ in items:
                d.text((x + i * 0.7, y + i), t, font=font, fill=scol)
    stroke = max(3, size // 26) if kind == "outline" else 0
    for x, y, t, c in items:
        if stroke:
            d.text((x, y), t, font=font, fill=c, stroke_width=stroke, stroke_fill=scol)
        else:
            d.text((x, y), t, font=font, fill=c)


def _fit_headline(lines, st, fname, sub, left):
    """Biggest font size at which the headline (and the subhook) still fits the text area."""
    max_w, max_h = W - 144, int(H * 0.31)
    size = min(int(112 * st["k"]), 140)
    while True:
        font = load_font(size, fname)
        lh = int(size * st["lh"])
        widths = [sum(font.getlength(t) for t, _ in segs) for segs in lines]
        extra = (int(size * 0.62) if sub and st["sub"] == "line" else 0) + (74 if sub and st["sub"] == "chip" else 0) + (26 if left else 0)
        total = lh * len(lines) + extra
        if (max(widths) <= max_w and total <= max_h) or size <= 40:
            return size, font, lh, widths, total
        size -= 4


def overlay_text(photo, headline_lines, subhook, style=None):
    """Full-bleed photo, tinted gradient at the bottom, big hook in this post's own font and colours, subhook, watermarks."""
    st = style or pick_style()
    _STYLE_USED[0] = st["name"]
    img = photo.convert("RGB")
    if img.size != (W, H):
        img = ImageOps.fit(img, (W, H), method=Image.LANCZOS)

    grad = Image.new("L", (1, H))
    px = grad.load()
    for y in range(H):
        f = y / H
        if f < 0.48:
            a = 0.0
        elif f < 0.72:
            a = 0.90 * _smooth((f - 0.48) / 0.24)
        else:
            a = 0.90 + 0.08 * ((f - 0.72) / 0.28)
        px[0, y] = int(255 * a)
    img.paste(Image.new("RGB", (W, H), st["tint"]), (0, 0), grad.resize((W, H)))

    left = st["align"] == "left"
    margin = 72
    sub = _ascii(subhook).upper().strip()
    lines = [parse_segments(_ascii(l).upper(), st) for l in headline_lines if l.strip()]
    size, font, lh, widths, total = _fit_headline(lines, st, st["font"], sub, left)
    if size < 60 and st["font"] != "Anton-Regular.ttf":   # a wide font would make this headline too small: keep the colours, use the condensed font
        size, font, lh, widths, total = _fit_headline(lines, st, "Anton-Regular.ttf", sub, left)

    draw = ImageDraw.Draw(img)
    y = H - 104 - total
    if left:  # short accent bar above the headline
        draw.rectangle([margin, y, margin + 120, y + 10], fill=st["accent"])
        y += 26
    if sub and st["sub"] == "chip":
        cf = load_font(34, "BebasNeue-Regular.ttf")
        cw = _spaced_width(sub, cf, 4) + 40
        cx = margin if left else (W - cw) / 2
        lum = 0.3 * st["accent"][0] + 0.59 * st["accent"][1] + 0.11 * st["accent"][2]
        draw.rounded_rectangle([cx, y, cx + cw, y + 52], radius=10, fill=st["accent"])
        _spaced(draw, cx + 20, y + 6, sub, cf, (0, 0, 0) if lum > 135 else (255, 255, 255), spacing=4, shadow=False)
        y += 74

    items = []
    for segs, width in zip(lines, widths):
        x = margin if left else (W - width) / 2
        for text, color in segs:
            items.append((x, y, text, color))
            x += font.getlength(text)
        y += lh
    _headline_layers(img, items, font, st)

    draw = ImageDraw.Draw(img)
    if sub and st["sub"] == "line":
        sf = load_font(max(30, int(size * 0.36)), "BebasNeue-Regular.ttf")
        sw = _spaced_width(sub, sf, 5)
        bar = 56
        total_w = sw + 2 * (bar + 18) if not left else bar + 18 + sw
        x0 = margin if left else (W - total_w) / 2
        yy = y + int(size * 0.17)
        mid = yy + sf.size * 0.55
        draw.rectangle([x0, mid - 2, x0 + bar, mid + 2], fill=st["accent"])
        xt = _spaced(draw, x0 + bar + 18, yy, sub, sf, st["text"], spacing=5)
        if not left:
            draw.rectangle([xt + 13, mid - 2, xt + 13 + bar, mid + 2], fill=st["accent"])

    # watermarks: page name (bottom-left) and the AI label (bottom-right)
    if PAGE_NAME:
        wf = load_font(26, "BebasNeue-Regular.ttf")
        draw.ellipse([30, H - 46, 46, H - 30], fill=st["accent"])
        x = 58
        for c in PAGE_NAME.upper():
            draw.text((x + 1, H - 48), c, font=wf, fill=(0, 0, 0))
            draw.text((x, H - 49), c, font=wf, fill=(238, 238, 238))
            x += wf.getlength(c) + 3
    if WATERMARK:
        af = load_font(24, "BebasNeue-Regular.ttf")
        label = WATERMARK.upper()
        aw = _spaced_width(label, af, 2)
        _spaced(draw, W - aw - 30, H - 48, label, af, (205, 205, 205), spacing=2, shadow=False)
    return img


# ----------------------------------------------------------------- telegram
def tg(method, **kwargs):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", timeout=120, **kwargs)
    if not r.ok:
        raise RuntimeError(f"telegram {method} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def send_to_telegram(img, post):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=94, subsampling=0)
    buf.seek(0)
    # 1) image alone, 2) full caption as a normal message (no 1024-char caption limit)
    tg("sendPhoto", data={"chat_id": TG_CHAT}, files={"photo": ("post.jpg", buf)})
    tg("sendMessage", data={"chat_id": TG_CHAT, "text": post["caption"].strip()[:4000]})
    if SEND_NOTES:
        notes = (
            f"Fact check notes\n"
            f"Confidence: {post.get('confidence')}\n"
            f"Fact: {post.get('fact')}\n"
            f"Sources to verify: {', '.join(post.get('sources') or [])}\n"
        )
        if post.get("issues"):
            notes += "Checker remarks: " + "; ".join(map(str, post["issues"])) + "\n"
        notes += "Image rights: AI-generated recreation, no real people or logos."
        tg("sendMessage", data={"chat_id": TG_CHAT, "text": notes[:4000]})


# ----------------------------------------------------------------- facebook
def send_to_facebook(img, post):
    """Publish image + caption together as ONE photo post on the Page."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=94, subsampling=0)
    buf.seek(0)
    caption = post["caption"].strip()
    if FB_DISCLOSURE:
        caption += "\n\n" + FB_DISCLOSURE
    r = requests.post(
        f"https://graph.facebook.com/{FB_VERSION}/{FB_PAGE_ID}/photos",
        data={"caption": caption, "published": "true", "access_token": FB_PAGE_TOKEN},
        files={"source": ("post.jpg", buf, "image/jpeg")},
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"facebook failed: HTTP {r.status_code} {r.text[:400]}")
    data = r.json()
    log("facebook post id:", data.get("post_id") or data.get("id"))


def facebook_due():
    if not (FB_PAGE_ID and FB_PAGE_TOKEN):
        return False
    return datetime.now(timezone.utc).hour % FB_EVERY_HOURS == 0


# ----------------------------------------------------------------- main
def main():
    missing = [n for n, v in (("POLLINATIONS_API_KEY (or CF_ACCOUNT_ID and CF_API_TOKEN)", KEY or (CF_ACCOUNT_ID and CF_API_TOKEN)), ("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT),
                              ("OPENROUTER_API_KEYS", ",".join(OR_KEYS) or ("-" if CF_TEXT else ""))) if not v]
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)}")

    load_keystate()
    setup_models()
    try:
        run()
    finally:
        log(f"OpenRouter keys usable now: {len(keys_usable())}/{len(OR_KEYS)}")
        tot = _NEURONS["text"] + _NEURONS["image"]
        log(f"WORKERS AI USAGE THIS RUN (estimate): text {_NEURONS['text']:.0f} + images {_NEURONS['image']:.0f} = {tot:.0f} neurons "
            f"({_NEURONS['calls']} text calls). Daily free limit 10000. Check the real number in the Cloudflare dashboard.")
        save_keystate()


def run():
    history = load_history()
    recent = [h["topic"] for h in history[-40:]]
    used = {h.get("source") for h in history if h.get("source")}
    RECENT_STYLES[:] = [h.get("style") for h in history[-6:] if h.get("style")]
    try:
        post = build_post(recent, used)  # always returns something unless Wikipedia itself is unreachable
    except RuntimeError as e:
        log("NO POST THIS RUN (Wikipedia could not be reached):", e)
        return
    log("topic:", post["topic"])

    try:
        photo = generate_image(post)
    except Exception as e:  # noqa: BLE001
        log("no picture could be made, using the text card:", e)
        photo = fallback_background()
    img = overlay_text(photo, post["headline_lines"], post.get("subhook", "") if SHOW_SUBHOOK else "")

    sent = False
    try:
        send_to_telegram(img, post)
        sent = True
    except Exception as e:  # noqa: BLE001
        log("TELEGRAM ERROR:", e)
    if facebook_due():
        try:
            send_to_facebook(img, post)
            sent = True
        except Exception as e:  # noqa: BLE001
            log("FACEBOOK ERROR:", e)
    else:
        log("facebook skipped this hour (not configured or not due)")

    if sent:
        history.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                "topic": post["topic"],
                "source": post.get("source_title"),
                "habitat": post.get("habitat"),
                "style": _STYLE_USED[0],
            }
        )
        save_history(history)
        log("done")
    else:
        log("NOTHING WAS DELIVERED: check the Telegram and Facebook errors above")


if __name__ == "__main__":
    main()
