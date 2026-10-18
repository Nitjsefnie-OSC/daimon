"""The mounted GitHub helper selects only a granted repository's placeholder."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from daimon.core.credential_env import github_repo_script_bytes, render_github_repo_map


def _run(
    script: Path, env: dict[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        cwd=script.parent,
        check=False,
    )


def test_per_repo_helper_selects_exact_placeholder_and_configures_clone(tmp_path: Path) -> None:
    script = tmp_path / "github-repo.sh"
    script.write_bytes(github_repo_script_bytes())
    (tmp_path / "github-repos.tsv").write_bytes(
        render_github_repo_map(
            (("OwnerA/RepoA", "GH_TOKEN_I101_0"), ("OwnerB/RepoB", "GH_TOKEN_I202_0"))
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "$GH_TOKEN" > "$CAPTURE_DIR/token"\n'
        'printf \'%s\\n\' "$@" > "$CAPTURE_DIR/args"\n'
        'if [ "$1" = repo ] && [ "$2" = clone ]; then\n'
        "  dir=${4:-${3##*/}}\n"
        '  git init -q "$dir"\n'
        '  git -C "$dir" remote add origin "https://github.com/$3.git"\n'
        "fi\n"
    )
    fake_gh.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CAPTURE_DIR": str(capture),
            "GH_TOKEN_I101_0": "opaque-placeholder-A",
            "GH_TOKEN_I202_0": "opaque-placeholder-B",
            "GH_TOKEN": "wrong-alias",
        }
    )
    global_config = tmp_path / "global-gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(global_config),
            "credential.https://github.com.helper",
            "!f() { echo username=wrong; echo password=wrong-inherited-token; }; f",
        ],
        check=True,
    )
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )

    selected = _run(script, env, "ownera/REPOA.git", "api", "repos/OwnerA/RepoA")
    assert selected.returncode == 0
    assert (capture / "token").read_text() == "opaque-placeholder-A"
    assert (capture / "args").read_text().splitlines() == ["api", "repos/OwnerA/RepoA"]
    assert "opaque-placeholder" not in selected.stdout + selected.stderr

    selected_b = _run(script, env, "OWNERB/repob", "repo", "view", "OwnerB/RepoB")
    assert selected_b.returncode == 0
    assert (capture / "token").read_text() == "opaque-placeholder-B"
    assert "opaque-placeholder" not in selected_b.stdout + selected_b.stderr

    (capture / "token").unlink()
    denied = _run(script, env, "other/hidden", "api", "repos/other/hidden")
    assert denied.returncode == 2
    assert "other/hidden is not granted to this agent" in denied.stderr
    assert not (capture / "token").exists(), "gh must not run for an uncovered repo"
    assert "opaque-placeholder" not in denied.stdout + denied.stderr

    missing_variable = _run(
        script,
        {key: value for key, value in env.items() if key != "GH_TOKEN_I202_0"},
        "OwnerB/RepoB",
        "api",
        "repos/OwnerB/RepoB",
    )
    assert missing_variable.returncode == 2
    assert "OwnerB/RepoB is not granted to this agent" in missing_variable.stderr
    assert not (capture / "token").exists()

    for command in ("auth", "config", "alias", "extension", "secret", "variable"):
        blocked = _run(script, env, "OwnerB/RepoB", command, "token")
        assert blocked.returncode == 2
        assert f"gh {command} is unavailable" in blocked.stderr
        assert not (capture / "token").exists()
    for args in (
        ("--help=false", "auth", "token"),
        ("-R", "OwnerB/RepoB", "auth", "token"),
        ("--repo=OwnerB/RepoB", "api", "user"),
        ("--help",),
        ("unknown",),
    ):
        blocked = _run(script, env, "OwnerB/RepoB", *args)
        assert blocked.returncode == 2
        assert "usage: sh github-repo.sh" in blocked.stderr
        assert not (capture / "token").exists()

    clone_dir = tmp_path / "clone-b"
    clone = _run(script, env, "OwnerB/RepoB", "clone", str(clone_dir))
    assert clone.returncode == 0, clone.stderr
    assert (capture / "token").read_text() == "opaque-placeholder-B"
    helpers = subprocess.check_output(
        [
            "git",
            "-C",
            str(clone_dir),
            "config",
            "--local",
            "--get-all",
            "credential.https://github.com.helper",
        ],
        text=True,
    ).splitlines()
    assert helpers == ["", "!sh /mnt/session/uploads/github-repo.sh --git-credential"]
    assert (
        subprocess.check_output(
            ["git", "-C", str(clone_dir), "config", "--get", "credential.useHttpPath"],
            text=True,
        ).strip()
        == "true"
    )
    # Substitute the local test asset path for the sandbox mount path, keeping
    # the empty local entry that resets the inherited global helper.
    subprocess.run(
        [
            "git",
            "-C",
            str(clone_dir),
            "config",
            "--unset-all",
            "credential.https://github.com.helper",
        ],
        check=True,
    )
    for value in ("", f"!sh {script} --git-credential"):
        subprocess.run(
            [
                "git",
                "-C",
                str(clone_dir),
                "config",
                "--add",
                "credential.https://github.com.helper",
                value,
            ],
            check=True,
        )
    credential = subprocess.run(
        ["git", "-C", str(clone_dir), "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=OwnerB/RepoB.git\n\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert credential.returncode == 0
    assert "username=x-access-token\n" in credential.stdout
    assert "password=opaque-placeholder-B\n" in credential.stdout
    assert "wrong-inherited-token" not in credential.stdout
    for path in ("OwnerA/RepoA", "not/granted"):
        denied_git = subprocess.run(
            ["git", "-C", str(clone_dir), "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert denied_git.returncode != 0
        assert "password=" not in denied_git.stdout
        assert "wrong-inherited-token" not in denied_git.stdout
    assert "opaque-placeholder" not in clone.stdout + clone.stderr


def test_git_credential_mode_only_answers_exact_granted_https_path(tmp_path: Path) -> None:
    script = tmp_path / "github-repo.sh"
    script.write_bytes(github_repo_script_bytes())
    (tmp_path / "github-repos.tsv").write_bytes(
        render_github_repo_map((("OwnerB/RepoB", "GH_TOKEN_I202_0"),))
    )
    env = {**os.environ, "GH_TOKEN_I202_0": "opaque-placeholder-B"}
    valid = _run(
        script,
        env,
        "--git-credential",
        "get",
        stdin="protocol=https\nhost=github.com\npath=ownerb/REPOB.git\n\n",
    )
    assert valid.returncode == 0
    assert valid.stdout == "username=x-access-token\npassword=opaque-placeholder-B\n"
    for request in (
        "protocol=http\nhost=github.com\npath=OwnerB/RepoB\n\n",
        "protocol=https\nhost=evil.example\npath=OwnerB/RepoB\n\n",
        "protocol=https\nhost=github.com\npath=OwnerB/RepoB/extra\n\n",
        "protocol=https\nhost=github.com\npath=other/repo\n\n",
        "protocol=https\nhost=github.com\n\n",
    ):
        result = _run(script, env, "--git-credential", "get", stdin=request)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
    for method in ("store", "erase"):
        result = _run(script, env, "--git-credential", method, stdin="anything\n\n")
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
