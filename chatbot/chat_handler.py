import os
import requests
from dotenv import load_dotenv

from prompt_builder import (
    build_system_prompt,
    build_classifier_prompt,
    build_step_mode_prompt,
    build_fallback_prompt,
    build_error_context_prompt,
)

# Load .env from project root (tries current dir and one level up)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    raise ValueError(
        "\n\n❌ OPENROUTER_API_KEY not found!\n"
        "Add this to your .env file:\n"
        "OPENROUTER_API_KEY=sk-or-your-key-here\n"
        "Get a free key at: https://openrouter.ai\n"
    )

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "meta-llama/llama-3.3-70b-instruct" # free model, no billing needed

GAP_PHRASES = [
    "could not find this in the provided documentation",
    "there are no further documented solutions",
    "not covered in the documentation",
    "not mentioned in the documentation",
    "i will alert your developer now",
    "please contact your developer",
]


def _call_openrouter(system_instruction: str, history: list, message: str) -> str:
    messages = [{"role": "system", "content": system_instruction}]
    for msg in history:
        role = "user" if msg["role"] == "user" else "assistant"
        messages.append({"role": role, "content": msg["content"]})
    messages.append({"role": "user", "content": message})

    # Free models can be rate-limited — retry up to 3 times
    import time
    for attempt in range(3):
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "http://localhost:5000",
                "X-Title":       "Website Support Chatbot",
            },
            json={
                "model":       MODEL,
                "messages":    messages,
                "temperature": 0.3,
                "max_tokens":  1500,
            },
            timeout=30,
        )

        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]

        if response.status_code == 429:
            wait = 2 ** attempt  # 1s, 2s, 4s
            print(f"Rate limited, retrying in {wait}s... (attempt {attempt+1}/3)")
            time.sleep(wait)
            continue

        raise Exception(f"OpenRouter API error {response.status_code}: {response.text}")

    raise Exception("Rate limit exceeded after 3 retries. Please try again in a moment.")

def _is_doc_gap(reply: str) -> bool:
    lower = reply.lower()
    return any(phrase in lower for phrase in GAP_PHRASES)


# ── Public functions — one per chat mode ──────────────────────────

def get_classifier_response(history: list, user_message: str) -> tuple[str, bool]:
    system   = build_classifier_prompt()
    reply    = _call_openrouter(system, history, user_message)
    complete = "let me find the solution for you" in reply.lower()
    return reply, complete


def get_step_response(history: list, user_message: str, problem_summary: str) -> tuple[str, bool]:
    system = build_step_mode_prompt(problem_summary)
    reply  = _call_openrouter(system, history, user_message)
    return reply, _is_doc_gap(reply)


def get_fallback_response(history: list, previous_solution: str) -> tuple[str, bool]:
    system  = build_system_prompt()
    message = build_fallback_prompt(previous_solution)
    reply   = _call_openrouter(system, history, message)
    return reply, _is_doc_gap(reply)


def get_error_response(history: list, user_message: str, error_text: str) -> tuple[str, bool]:
    system  = build_system_prompt()
    message = build_error_context_prompt(error_text, user_message)
    reply   = _call_openrouter(system, history, message)
    return reply, _is_doc_gap(reply)


def get_general_response(history: list, user_message: str) -> tuple[str, bool]:
    system = build_system_prompt()
    reply  = _call_openrouter(system, history, message=user_message)
    return reply, _is_doc_gap(reply)
