from __future__ import annotations

from hyperrag.hyperrag_v2 import HyperRAGV2


def test_fallback_chunk_from_mix_hit_preserves_retrieval_scores() -> None:
    runtime = HyperRAGV2.__new__(HyperRAGV2)

    chunk = runtime._fallback_chunk_from_mix_hit(
        {
            "chunk_id": "chunk-1",
            "text": "电梯出坑前应执行标准程序。",
            "score": 42.0,
            "vector_score": 0.87,
            "metadata": {},
        }
    )

    assert chunk["score"] == 42.0
    assert chunk["vector_score"] == 0.87
