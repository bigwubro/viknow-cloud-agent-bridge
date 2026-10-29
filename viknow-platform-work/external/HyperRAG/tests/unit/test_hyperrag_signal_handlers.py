import signal

from hyperrag import HyperRAGConfig, HyperRAGV2


def test_hyperrag_v2_does_not_install_process_signal_handlers_by_default(
    monkeypatch,
) -> None:
    registered_handlers = []

    def fake_signal(signum, handler):
        registered_handlers.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)

    HyperRAGV2(HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace"))

    assert registered_handlers == []
