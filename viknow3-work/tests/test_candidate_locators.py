"""Search hits must keep the document asset id the inspect gate checks."""

from agents.runtime.core.runtime.evidence_ledger import _candidate_locators


def test_document_search_hit_keeps_full_doc_id_and_file_part_asset_id():
    locators = _candidate_locators(
        {
            "library_id": "e2e-artifacts-20261022",
            "evidence": [
                {
                    "id": "chunk-8dca609df47870528caaa5264380d5e1",
                    "title": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3",
                    "metadata": {
                        "full_doc_id": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3",
                        "chunk_id": "chunk-8dca609df47870528caaa5264380d5e1",
                    },
                    "parts": [
                        {"kind": "text", "text": "ViKnow 索引验收", "metadata": {}},
                        {
                            "kind": "file",
                            "metadata": {"asset_id": "a9bdb3a4-558b-43af-a6e8-7545c40d1db3"},
                        },
                    ],
                }
            ],
        }
    )

    assert locators
    assert {locator.asset_id for locator in locators} == {
        "a9bdb3a4-558b-43af-a6e8-7545c40d1db3"
    }
    assert {locator.segment_id for locator in locators} == {
        "chunk-8dca609df47870528caaa5264380d5e1"
    }
