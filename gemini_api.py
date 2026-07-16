import os
from typing import Iterable

from langchain_core.embeddings import Embeddings


def gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")


def new_client():
    from google import genai

    key = gemini_key()
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY")
    return genai.Client(api_key=key)


class GoogleGenAIEmbeddings(Embeddings):
    """LangChain embedding adapter backed by the current google-genai SDK."""

    def __init__(self, model: str = "gemini-embedding-001") -> None:
        self.model = model.removeprefix("models/")

    def _embed(self, texts: Iterable[str], task_type: str) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        from google.genai import types

        client = new_client()
        try:
            response = client.models.embed_content(
                model=self.model,
                contents=values,
                config=types.EmbedContentConfig(task_type=task_type),
            )
            return [list(item.values or []) for item in (response.embeddings or [])]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        embeddings = self._embed([text], "RETRIEVAL_QUERY")
        if not embeddings:
            raise RuntimeError("Gemini embedding returned no vector")
        return embeddings[0]


def generate_text(model: str, prompt: str, temperature: float = 0) -> str:
    from google.genai import types

    client = new_client()
    try:
        response = client.models.generate_content(
            model=model.removeprefix("models/"),
            contents=prompt,
            config=types.GenerateContentConfig(temperature=temperature),
        )
        return str(response.text or "").strip()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
