"""Security defects: untrusted artifact metadata reaching a root shell.

``module.prop`` comes from a ZIP the operator imported (possibly downloaded from
an arbitrary URL). Its ``id`` is interpolated into device shell commands without
validation, unlike ``params.package`` which goes through ``_validate_package``.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.instrumentation import ProfileStep, StepAction
from tests.qa.qa_support import ScriptedAdb, ShellAdb, magisk_zip, open_service, target


def test_module_prop_id_from_an_imported_zip_is_executed_by_the_root_shell(
    tmp_path: Path,
) -> None:
    """An imported module's ``module.prop`` id is interpolated into a root command.

    The ZIP is inert at import time, but deploying it runs
    ``[ -d /data/adb/modules/<id> ] && echo yes`` as root. ``<id>`` is taken from
    the archive and never validated, so a crafted id such as
    ``evil;touch /data/adb/pwned;#`` runs an arbitrary second command as root on
    every targeted AVD. The same unvalidated value reaches ``enable_module``,
    ``disable_module`` and ``remove_module`` through ``_module_identifier``.
    """
    adb = ShellAdb()  # every adb command "succeeds"; ';' runs a second command
    with open_service(tmp_path, adb) as service:
        record = service.import_upload(
            "evil.zip",
            magisk_zip(
                id="evil;touch /data/adb/pwned;#",
                name="Totally normal module",
            ),
        )
        step = ProfileStep(
            id="install-evil",
            action=StepAction.INSTALL_MODULE,
            module_id=record.id,
        )

        service._step_install_module(step, target())

    injected = [command for command in adb.executed if "touch /data/adb/pwned" in command]
    assert injected == [], (
        "the archive-controlled module.prop id was executed as a second root "
        f"command: {injected} (raw commands: {[c for c in adb.commands if 'pwned' in c]})"
    )


def test_import_url_persists_credentials_embedded_in_the_source_url(
    tmp_path: Path,
) -> None:
    """Provenance stores the raw URL, so ``user:password@`` reaches the database.

    ``ModuleRecord.source_url`` is persisted verbatim and returned by
    ``GET /instrumentation/modules``. The project's redaction choke point is
    never applied to it, so a credentialed import URL (userinfo or a signed
    query string) is stored and served to every authenticated reader.
    """
    payload = magisk_zip()

    def http_get(url: str, limit: int) -> bytes:
        return payload

    with open_service(tmp_path, ScriptedAdb(), http_get=http_get) as service:
        service.import_url("https://qa-user:s3cret-value@downloads.example.invalid/alpha.zip")
        stored = service.list_modules()[0]

    assert "s3cret-value" not in stored.source_url, (
        "the import URL's password was persisted in module provenance and is now "
        f"returned by the API: {stored.source_url!r}"
    )
