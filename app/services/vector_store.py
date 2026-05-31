"""
Vector store service — ChromaDB for semantic deduplication.

Why semantic dedup matters:
    SHA-256 hash dedup catches EXACT duplicates. But these two leads
    are clearly the same intent, different words:
    - "Need AI automation for customer support tickets"
    - "Looking for intelligent ticket automation with AI"

    Semantic dedup embeds the message text and searches for cosine
    similarity above a threshold (default: 0.85). This catches
    near-duplicates that hash-based dedup misses.

Design constraints:
    - ChromaDB runs in-process (no extra Docker container)
    - Persists to a mounted volume for durability across restarts
    - If embedding or ChromaDB fails, the lead PASSES THROUGH
      (fail-open, not fail-closed) — semantic dedup is a bonus check
    - Uses Gemini's text-embedding-004 model (already have the API key)
"""

import os
from loguru import logger
from app.core.config import settings


def _get_chroma_client():
    """Get or create a persistent ChromaDB client.

    Uses in-process client with file-based persistence.
    No extra Docker container needed.
    """
    try:
        import chromadb
        persist_dir = settings.CHROMA_PERSIST_DIR
        os.makedirs(persist_dir, exist_ok=True)
        client = chromadb.PersistentClient(path=persist_dir)
        return client
    except Exception as e:
        logger.warning(f"ChromaDB initialization failed: {e}")
        return None


def _get_collection():
    """Get or create the lead_messages collection."""
    client = _get_chroma_client()
    if client is None:
        return None
    try:
        return client.get_or_create_collection(
            name="lead_messages",
            metadata={"description": "Lead message embeddings for semantic dedup"},
        )
    except Exception as e:
        logger.warning(f"ChromaDB collection access failed: {e}")
        return None


def _embed_text(text: str) -> list[float] | None:
    """Generate embedding using Gemini's text-embedding-004 model.

    Returns None on failure or if no API key is configured.
    """
    try:
        key = settings.GOOGLE_API_KEY
        if not key or key == "your-gemini-api-key-here":
            logger.debug("Skipping embedding — no GOOGLE_API_KEY configured")
            return None
        from google import genai
        client = genai.Client(api_key=key)
        result = client.models.embed_content(
            model="gemini-embedding-2",
            contents=text,
        )
        return result.embeddings[0].values
    except Exception as e:
        logger.warning(f"Embedding generation failed: {e}")
        return None


def add_lead_embedding(lead_id: str, message: str) -> bool:
    """Store a lead's message embedding in ChromaDB.

    Called after successful enrichment to build the dedup index.

    Args:
        lead_id: UUID string of the lead.
        message: The lead's message text to embed.

    Returns:
        bool: True if stored successfully, False on failure.
    """
    if not settings.ENABLE_SEMANTIC_DEDUP:
        return False

    try:
        collection = _get_collection()
        if collection is None:
            return False

        embedding = _embed_text(message)
        if embedding is None:
            return False

        collection.add(
            ids=[lead_id],
            embeddings=[embedding],
            documents=[message],
            metadatas=[{"lead_id": lead_id}],
        )
        logger.debug("Lead embedding stored", lead_id=lead_id)
        return True
    except Exception as e:
        logger.warning(f"Failed to store lead embedding: {e}", lead_id=lead_id)
        return False


def find_similar_leads(
    message: str,
    threshold: float | None = None,
    max_results: int = 3,
) -> list[dict]:
    """Search for semantically similar leads.

    Args:
        message: The message text to search for.
        threshold: Minimum similarity score (0-1). Defaults to config value.
        max_results: Maximum number of similar leads to return.

    Returns:
        list[dict]: List of similar leads with lead_id, similarity, and document.
        Empty list if semantic dedup is disabled or on failure.
    """
    if not settings.ENABLE_SEMANTIC_DEDUP:
        return []

    threshold = threshold or settings.SEMANTIC_SIMILARITY_THRESHOLD

    try:
        collection = _get_collection()
        if collection is None:
            return []

        # Check if collection has any documents
        if collection.count() == 0:
            return []

        embedding = _embed_text(message)
        if embedding is None:
            return []

        results = collection.query(
            query_embeddings=[embedding],
            n_results=min(max_results, collection.count()),
            include=["documents", "distances", "metadatas"],
        )

        similar = []
        if results and results["distances"] and results["distances"][0]:
            for i, distance in enumerate(results["distances"][0]):
                # ChromaDB returns L2 distance by default
                # Convert to similarity: smaller distance = more similar
                # For normalized embeddings: similarity ≈ 1 - (distance² / 2)
                similarity = max(0, 1 - (distance ** 2 / 2))

                if similarity >= threshold:
                    similar.append({
                        "lead_id": results["ids"][0][i],
                        "similarity": round(similarity, 4),
                        "document": results["documents"][0][i] if results["documents"] else None,
                    })

        return similar

    except Exception as e:
        logger.warning(f"Semantic search failed (non-fatal): {e}")
        return []
