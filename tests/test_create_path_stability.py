"""Regressions for create admission ownership and post-commit behavior."""

import pytest

from docker_host_manager import DockerHostManager


def _manager_with_context(limit: int = 1) -> DockerHostManager:
    manager = DockerHostManager()
    manager._context_configs = {"alpha": "unix:///fake.sock"}
    manager._init_semaphores(limit)
    return manager


def test_matching_reload_preserves_inflight_semaphore_generation():
    manager = _manager_with_context(limit=1)
    token = manager.acquire_semaphore("alpha", timeout=0)
    assert token is not None

    manager._init_semaphores(1)

    assert manager._semaphores["alpha"] is token
    with pytest.raises(Exception, match="server busy"):
        manager.acquire_semaphore("alpha", timeout=0)

    manager.release_semaphore(token)
    replacement_token = manager.acquire_semaphore("alpha", timeout=0)
    assert replacement_token is token
    manager.release_semaphore(replacement_token)


def test_release_targets_acquired_object_after_limit_reload():
    manager = _manager_with_context(limit=1)
    old_token = manager.acquire_semaphore("alpha", timeout=0)
    assert old_token is not None

    manager._init_semaphores(2)
    first_new_token = manager.acquire_semaphore("alpha", timeout=0)
    assert first_new_token is not None
    assert first_new_token is not old_token

    # Releasing an in-flight acquisition from the old generation must not add
    # capacity to the new semaphore selected by context name.
    manager.release_semaphore(old_token)
    second_new_token = manager.acquire_semaphore("alpha", timeout=0)
    assert second_new_token is first_new_token
    with pytest.raises(Exception, match="server busy"):
        manager.acquire_semaphore("alpha", timeout=0)

    manager.release_semaphore(first_new_token)
    manager.release_semaphore(second_new_token)
