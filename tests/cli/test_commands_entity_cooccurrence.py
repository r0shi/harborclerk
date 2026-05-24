from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.entity_cooccurrence.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.entity_cooccurrence.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_entity_cooccurrence_defaults():
    rc, client = _run(["entity-cooccurrence", "Acme Corp", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["entity_text"] == "Acme Corp"
    assert called_args["limit"] == 20
    assert "entity" not in called_args
    assert "k" not in called_args


def test_entity_cooccurrence_k_forwarded():
    rc, client = _run(["entity-cooccurrence", "Alice", "-k", "5", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["limit"] == 5
    assert "k" not in called_args


def test_entity_cooccurrence_entity_type_forwarded():
    rc, client = _run(["entity-cooccurrence", "Alice", "--entity-type", "PERSON", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["entity_type"] == "PERSON"


def test_entity_cooccurrence_scope_default_chunk():
    rc, client = _run(["entity-cooccurrence", "Acme Corp", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["scope"] == "chunk"


def test_entity_cooccurrence_scope_document_forwarded():
    rc, client = _run(["entity-cooccurrence", "Acme Corp", "--scope", "document", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["scope"] == "document"


def test_entity_cooccurrence_cooccur_type_forwarded():
    rc, client = _run(["entity-cooccurrence", "Alice", "--cooccur-type", "ORG", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["cooccur_type"] == "ORG"


def test_entity_cooccurrence_doc_id_forwarded():
    doc_id = "11111111-1111-1111-1111-111111111111"
    rc, client = _run(["entity-cooccurrence", "Alice", "--doc-id", doc_id, "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["doc_id"] == doc_id
