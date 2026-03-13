"""
Static analysis tests for admin.html template.
Proves the delete modal CSS/JS fix is correctly applied.
No server or database required.
"""
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent.parent / "app" / "templates" / "admin.html"


def _html():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


class TestDeleteModalTemplateIntegrity:

    def test_modal_uses_inline_display_none_not_tailwind_hidden(self):
        """Modal must use style='display:none' — Tailwind hidden class causes JIT CDN failure."""
        html = _html()
        modal_start = html.index('id="delete-confirm-modal"')
        modal_tag = html[modal_start - 5: modal_start + 400]

        assert "display:none" in modal_tag, (
            "Modal delete-confirm-modal must have style='display:none'. "
            "Tailwind JIT CDN does not generate .flex if it only appears in JS."
        )
        assert '"hidden' not in modal_tag.split("style=")[0], (
            "Modal must NOT have 'hidden' Tailwind class — it prevents flex display."
        )

    def test_confirm_delete_user_uses_style_display(self):
        """confirmDeleteUser must set modal.style.display = 'flex', not classList manipulation."""
        html = _html()
        fn_start = html.index("function confirmDeleteUser")
        fn_block = html[fn_start: fn_start + 500]

        assert "modal.style.display = 'flex'" in fn_block, (
            "confirmDeleteUser must use style.display = 'flex' to show the modal."
        )
        assert "classList.add('flex')" not in fn_block, (
            "classList.add('flex') fails with Tailwind CDN JIT — must use style.display."
        )

    def test_close_delete_modal_uses_style_display(self):
        """closeDeleteModal must set style.display = 'none', not classList manipulation."""
        html = _html()
        fn_start = html.index("function closeDeleteModal")
        fn_block = html[fn_start: fn_start + 300]

        assert "style.display = 'none'" in fn_block, (
            "closeDeleteModal must use style.display = 'none' to hide the modal."
        )
        assert "classList.add('hidden')" not in fn_block, (
            "classList.add('hidden') should not be used — use style.display instead."
        )

    def test_execute_delete_user_calls_correct_api_endpoint(self):
        """executeDeleteUser must call DELETE /admin/users/${userId}."""
        html = _html()
        fn_start = html.index("async function executeDeleteUser")
        fn_block = html[fn_start: fn_start + 500]

        assert "method: 'DELETE'" in fn_block, "executeDeleteUser must use DELETE method."
        assert "/admin/users/${userId}" in fn_block, (
            "DELETE must target /admin/users/{id}, not another endpoint."
        )
        assert "window.location.reload()" in fn_block, (
            "On success, page must reload to reflect the deletion."
        )

    def test_delete_button_triggers_correct_function(self):
        """Both desktop and mobile Excluir buttons must call confirmDeleteUser."""
        html = _html()
        occurrences = html.count("confirmDeleteUser")

        # 1 function definition + 2 onclick attributes (desktop table + mobile card)
        assert occurrences >= 3, (
            f"Expected at least 3 references to confirmDeleteUser, found {occurrences}. "
            "Desktop and/or mobile Excluir button may be missing onclick handler."
        )

    def test_detail_modal_also_uses_inline_style(self):
        """detail-modal must also use display:none for consistency."""
        html = _html()
        modal_start = html.index('id="detail-modal"')
        modal_tag = html[modal_start - 5: modal_start + 200]

        assert "display:none" in modal_tag, (
            "detail-modal must also use style='display:none'."
        )


class TestOpenDetailJsIntegrity:
    """Ensure the USERS embedded object was removed and openDetail uses async API fetch."""

    def test_no_jinja2_space_brace_syntax(self):
        """Template must not contain '{ {' — Jinja2 ignores it and JS SyntaxError results."""
        html = _html()
        assert "{ {" not in html, (
            "Found broken Jinja2 syntax '{ { expr } }' in admin.html. "
            "This renders literally, causing a JavaScript SyntaxError that kills all JS functions."
        )

    def test_no_embedded_users_object(self):
        """The static USERS JS object must be removed — it relied on broken Jinja2 syntax."""
        html = _html()
        assert "const USERS = {" not in html, (
            "Found static USERS JS object in admin.html. "
            "This object used broken '{ { expr } }' Jinja2 syntax and must be replaced with async fetch."
        )

    def test_open_detail_is_async(self):
        """openDetail must be declared as async function to support fetch()."""
        html = _html()
        assert "async function openDetail(userId)" in html, (
            "openDetail must be an async function to call await fetch()."
        )

    def test_open_detail_fetches_admin_api(self):
        """openDetail must fetch /admin/users/ endpoint, not read a static object."""
        html = _html()
        fn_start = html.index("async function openDetail(userId)")
        fn_block = html[fn_start: fn_start + 600]

        assert "/admin/users/" in fn_block, (
            "openDetail must fetch /admin/users/{userId} to load user data dynamically."
        )

    def test_open_detail_shows_stats(self):
        """openDetail must render transaction stats from API response."""
        html = _html()
        fn_start = html.index("async function openDetail(userId)")
        fn_block = html[fn_start: fn_start + 6000]

        assert "u.stats.pix_sent_count" in fn_block, (
            "openDetail must render pix_sent_count from stats returned by the API."
        )
        assert "u.stats.pix_received_count" in fn_block, (
            "openDetail must render pix_received_count from stats returned by the API."
        )

    def test_open_detail_renders_recent_pix(self):
        """openDetail must render the recent_pix table from API response."""
        html = _html()
        fn_start = html.index("async function openDetail(userId)")
        fn_block = html[fn_start: fn_start + 3000]

        assert "u.recent_pix" in fn_block, (
            "openDetail must iterate u.recent_pix to render the recent transactions table."
        )
