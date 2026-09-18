import re

ITER_RE = re.compile(
    r"Iteration\((?P<num>\d+)\):\s*"
    r"(?P<ctx_req>\d+)\s+context requests,\s*"
    r"(?P<ctx_tok>\d+)\s+context tokens,\s*"
    r"(?P<gen_req>\d+)\s+generation requests,\s*"
    r"(?P<gen_tok>\d+)\s+generation tokens,\s*"
    r"iteration elapsed time:\s*"
    r"(?P<elapsed>[0-9.]+)\s*ms"
    r"(?:,\s*GPU KV cache usage:\s*[0-9.]+%)?"
    r"(?:,\s*encoder inputs:\s*(?P<enc_in>\d+),\s*"
    r"encoder output embeddings:\s*(?P<enc_emb>\d+))?"
)


def test_parses_encoder_after_kv_cache_usage() -> None:
    line = (
        "Engine 000: Iteration(8406559): 2 context requests, 2112 context tokens, "
        "3 generation requests, 3 generation tokens, iteration elapsed time: 257.32 ms, "
        "GPU KV cache usage: 0.6%, encoder inputs: 1, encoder output embeddings: 576"
    )
    m = ITER_RE.search(line)
    assert m is not None
    assert m.group("enc_in") == "1"
    assert m.group("enc_emb") == "576"


def test_parses_line_without_encoder_fields() -> None:
    line = (
        "Engine 000: Iteration(8410381): 1 context requests, 1056 context tokens, "
        "3 generation requests, 3 generation tokens, iteration elapsed time: 286.91 ms, "
        "GPU KV cache usage: 0.3%"
    )
    m = ITER_RE.search(line)
    assert m is not None
    assert m.group("enc_in") is None
    assert m.group("enc_emb") is None
