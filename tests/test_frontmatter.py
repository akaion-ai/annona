"""Vault frontmatter: the format, and the migration of existing vaults.

The point of this feature is portability, so the tests are written from the
outside in: what does the file on disk look like, and can something that is not
this runner read it?
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runner.brain import frontmatter
from runner.brain.manager import BrainManager
from runner.brain.models import SYNC_SYNCED

pytestmark = pytest.mark.unit


# ── The format ────────────────────────────────────────────────────────────────


class TestParsing:
    def test_a_plain_file_is_all_body(self):
        """Pre-migration notes must read as prose, not as a parse failure."""
        assert frontmatter.parse("just text") == ({}, "just text")

    def test_metadata_and_body_are_separated(self):
        meta, body = frontmatter.parse("---\ntitle: Hello\n---\n\nThe body.")

        assert meta == {"title": "Hello"}
        assert body == "The body."

    def test_a_body_round_trips_exactly(self):
        """No stray newline may accumulate: notes are read and written constantly."""
        body = "Line one.\n\nLine two.\n"
        rendered = frontmatter.dump({"title": "T"}, body)

        assert frontmatter.parse(rendered)[1] == body

    def test_malformed_yaml_does_not_lose_the_note(self):
        """Someone's prose must never be sacrificed to a stray colon."""
        text = "---\ntitle: [unclosed\n---\n\nImportant content."

        meta, body = frontmatter.parse(text)

        assert meta == {}
        assert "Important content." in body

    def test_a_scalar_frontmatter_is_ignored_not_crashed_on(self):
        meta, body = frontmatter.parse("---\njust a string\n---\n\nbody")

        assert meta == {}
        assert body == "body"

    def test_a_body_containing_a_delimiter_survives(self):
        """`---` is also a markdown horizontal rule; it must not end the block."""
        rendered = frontmatter.dump({"title": "T"}, "before\n\n---\n\nafter")

        assert frontmatter.parse(rendered)[1] == "before\n\n---\n\nafter"


class TestWriting:
    def test_output_is_valid_yaml(self):
        """Other tools have to read this, so it is checked with a real parser."""
        rendered = frontmatter.dump(
            {"id": "abc", "title": "Q1: results", "tags": ["work", "q1"]}, "body"
        )
        raw = frontmatter.split(rendered)[0]

        loaded = yaml.safe_load(raw)
        assert loaded == {"id": "abc", "title": "Q1: results", "tags": ["work", "q1"]}

    def test_field_order_is_stable(self):
        """Stable bytes keep a vault diffable in Git."""
        rendered = frontmatter.dump(
            {"sync": "local_only", "title": "T", "id": "x", "tags": ["a"]}, ""
        )
        keys = [line.split(":")[0] for line in rendered.splitlines() if ":" in line]

        assert keys == ["id", "title", "tags", "sync"]

    def test_empty_values_are_omitted(self):
        """A note that never synced should not carry four blank cloud fields."""
        rendered = frontmatter.dump(
            {"id": "x", "title": "T", "tags": [], "cloud_message_id": None}, "body"
        )

        assert "tags" not in rendered
        assert "cloud_message_id" not in rendered

    @pytest.mark.parametrize("title", ["no", "yes", "true", "off", "null", "~"])
    def test_the_norway_problem_is_quoted_away(self, title: str):
        """A note titled "no" must not load back as the boolean False."""
        rendered = frontmatter.dump({"title": title}, "")

        assert yaml.safe_load(frontmatter.split(rendered)[0]) == {"title": title}

    @pytest.mark.parametrize(
        "title",
        ["Q1: results", "a #hash", "- leading dash", '"quoted"', "with 'apostrophe'", "  padded  "],
    )
    def test_awkward_titles_survive_a_round_trip(self, title: str):
        rendered = frontmatter.dump({"title": title}, "body")

        assert frontmatter.parse(rendered)[0]["title"] == title

    def test_no_metadata_means_no_delimiters(self):
        assert frontmatter.dump({}, "just body") == "just body"


# ── The vault ─────────────────────────────────────────────────────────────────


class TestVaultIsSelfDescribing:
    def test_a_new_note_carries_its_own_metadata(self, tmp_path: Path):
        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="Matter 118", content="Call the client", tags=["work"])
        brain.close()

        text = (tmp_path / "vault" / "notes" / f"{note.id}.md").read_text(encoding="utf-8")
        meta, body = frontmatter.parse(text)

        assert meta["title"] == "Matter 118"
        assert meta["tags"] == ["work"]
        assert meta["id"] == note.id
        assert body == "Call the client"

    def test_the_body_still_round_trips_through_the_manager(self, tmp_path: Path):
        """`Note.content` means the prose. Frontmatter must stay out of it."""
        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="T", content="Exactly this.", tags=[])

        assert brain.get(note.id).content == "Exactly this."
        brain.close()

    def test_sync_state_reaches_the_file(self, tmp_path: Path):
        """Delete the index and the vault still knows what was pushed."""
        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="T", content="c", tags=[])
        brain.mark_pending(note.id)
        brain.mark_synced(note.id, "msg-42", "cluster-7", "Legal")
        brain.close()

        meta = frontmatter.parse(
            (tmp_path / "vault" / "notes" / f"{note.id}.md").read_text(encoding="utf-8")
        )[0]

        assert meta["sync"] == SYNC_SYNCED
        assert meta["cloud_message_id"] == "msg-42"
        assert meta["cloud_cluster_name"] == "Legal"

    def test_editing_content_keeps_the_metadata(self, tmp_path: Path):
        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="Original", content="v1", tags=["keep"])
        brain.update(note.id, content="v2")
        brain.close()

        meta, body = frontmatter.parse(
            (tmp_path / "vault" / "notes" / f"{note.id}.md").read_text(encoding="utf-8")
        )

        assert body == "v2"
        assert meta["title"] == "Original"
        assert meta["tags"] == ["keep"]

    def test_hand_added_keys_are_preserved(self, tmp_path: Path):
        """The vault is meant to be edited by hand; the runner must not eat edits."""
        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="T", content="c", tags=[])
        path = tmp_path / "vault" / "notes" / f"{note.id}.md"

        meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        meta["project"] = "sovra"
        path.write_text(frontmatter.dump(meta, body), encoding="utf-8")

        brain.update(note.id, content="edited")
        brain.close()

        assert frontmatter.parse(path.read_text(encoding="utf-8"))[0]["project"] == "sovra"


class TestMigration:
    def test_an_old_vault_gains_frontmatter_on_open(self, tmp_path: Path):
        """Vaults written before this feature must not need a manual step."""
        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        note = brain.create(title="Old note", content="body text", tags=["legacy"])
        brain.close()

        # Rewind to the pre-migration format: body only.
        path = vault / "notes" / f"{note.id}.md"
        path.write_text("body text", encoding="utf-8")
        assert not frontmatter.has_frontmatter(path.read_text(encoding="utf-8"))

        reopened = BrainManager(vault)
        reopened.close()

        meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        assert meta["title"] == "Old note"
        assert meta["tags"] == ["legacy"]
        assert body == "body text"

    def test_migration_is_idempotent(self, tmp_path: Path):
        """Reopening a vault must be a no-op, not a rewrite of every file."""
        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        note = brain.create(title="T", content="c", tags=[])
        brain.close()

        path = vault / "notes" / f"{note.id}.md"
        before = path.read_text(encoding="utf-8")

        BrainManager(vault).close()

        assert path.read_text(encoding="utf-8") == before

    def test_a_note_whose_file_vanished_does_not_break_the_open(self, tmp_path: Path):
        """A vault someone pruned by hand must still open."""
        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        note = brain.create(title="T", content="c", tags=[])
        brain.close()

        (vault / "notes" / f"{note.id}.md").unlink()

        reopened = BrainManager(vault)
        assert reopened.get(note.id).content == ""
        reopened.close()


class TestFilesAreAuthoritative:
    """The index is a cache of the files; where they disagree, the file wins (#6)."""

    def test_retagging_a_note_in_another_editor_reaches_the_index(self, tmp_path: Path):
        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        note = brain.create(title="Draft", content="body", tags=["old"])
        brain.close()

        path = vault / "notes" / f"{note.id}.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("title: Draft", "title: Final").replace("[old]", "[new, filed]"),
            encoding="utf-8",
        )

        reopened = BrainManager(vault)
        again = reopened.get(note.id)
        assert again.title == "Final"
        assert again.tags == ["new", "filed"]
        assert reopened.find_note_by_tag("filed").id == note.id
        reopened.close()

    def test_a_markdown_file_dropped_into_the_vault_becomes_a_note(self, tmp_path: Path):
        vault = tmp_path / "vault"
        BrainManager(vault).close()
        (vault / "notes" / "meeting.md").write_text(
            "---\ntitle: Board meeting\ntags: [minutes]\n---\n\nApproved the budget\n",
            encoding="utf-8",
        )

        reopened = BrainManager(vault)
        note = reopened.get("meeting")
        assert note.title == "Board meeting"
        assert note.content == "Approved the budget\n"
        assert [n.id for n in reopened.search("budget")] == ["meeting"]
        reopened.close()

    def test_a_file_without_frontmatter_is_titled_by_its_name(self, tmp_path: Path):
        vault = tmp_path / "vault"
        BrainManager(vault).close()
        (vault / "notes" / "loose-thoughts.md").write_text("just prose\n", encoding="utf-8")

        reopened = BrainManager(vault)
        note = reopened.get("loose-thoughts")
        assert note.title == "loose-thoughts"
        assert note.tags == []
        assert note.sync_status == "local_only"
        reopened.close()

    def test_an_unknown_sync_state_in_a_file_is_not_trusted(self, tmp_path: Path):
        """A hand-typed `sync: synced` must not make the runner believe a push happened."""
        vault = tmp_path / "vault"
        BrainManager(vault).close()
        (vault / "notes" / "n.md").write_text(
            "---\ntitle: N\nsync: definitely-synced\n---\n\nx\n", encoding="utf-8"
        )

        reopened = BrainManager(vault)
        assert reopened.get("n").sync_status == "local_only"
        reopened.close()

    def test_synced_with_no_cloud_id_is_not_a_push_that_happened(self, tmp_path: Path):
        vault = tmp_path / "vault"
        BrainManager(vault).close()
        (vault / "notes" / "n.md").write_text(
            "---\ntitle: N\nsync: synced\n---\n\nx\n", encoding="utf-8"
        )

        reopened = BrainManager(vault)
        assert reopened.get("n").sync_status == "local_only"
        reopened.close()
