from charon.devop.devop_agents import infer_candidate_workstreams


def test_infer_candidate_workstreams_for_web_app():
    items = infer_candidate_workstreams('Build a web app that does X with frontend and backend')
    titles = [i['title'] for i in items]
    assert 'Frontend UI' in titles
    assert 'Backend API' in titles
    assert 'Testing & Integration' in titles


def test_infer_candidate_workstreams_generic():
    items = infer_candidate_workstreams('Build something useful')
    titles = [i['title'] for i in items]
    assert 'Core Implementation' in titles
    assert 'Verification & Testing' in titles


def _titles(prompt, project_root=None):
    return [i['title'] for i in infer_candidate_workstreams(prompt, project_root=project_root)]


def test_a_repo_of_another_platform_never_gets_ios_lanes(tmp_path):
    (tmp_path / 'go.mod').write_text('module example.com/svc\n')
    titles = _titles('Add an iOS client endpoint and SwiftUI-friendly JSON', tmp_path)
    assert 'Native Interface' not in titles
    assert not any('Speech' in t for t in titles)


def test_substrings_do_not_select_platform_lanes(tmp_path):
    (tmp_path / 'Cargo.toml').write_text('[package]\nname = "x"\n')
    # "scenarios" contains "ios"; "approach" contains "app".
    titles = _titles('Cover the failure scenarios with a better approach', tmp_path)
    assert titles[0] == 'Core Implementation'
    assert _titles('Build a backend API for the scheduler', tmp_path)[0] == 'Core Implementation'


def test_ios_lanes_come_from_an_ios_project_or_an_explicit_greenfield_request(tmp_path):
    ios = tmp_path / 'ios'
    (ios / 'App.xcodeproj').mkdir(parents=True)
    assert 'Native Interface' in _titles('Improve the onboarding flow', ios)
    empty = tmp_path / 'new'
    empty.mkdir()
    assert 'Native Interface' in _titles('Build an iOS app for habit tracking', empty)
    # The project-specific Pomemo lanes are gone.
    assert not any('Speech' in t for t in _titles('Build an iOS app', empty))


def _devop_engine(tmp_path, monkeypatch):
    from charon.context import system_prompt_builder
    from charon.devop import devop_agents
    from charon.providers import ModelInfo, StreamDelta, model_registry, worker_provider

    class Provider:
        async def stream(self, **kwargs):
            yield StreamDelta(type='done', text='{}')

    model = ModelInfo(provider='mock', model_id='mock', context_window=100_000)
    monkeypatch.setattr(worker_provider, 'request_worker_provider_for_background_flow', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(model_registry, 'get_shade_provider_and_model', lambda *a, **k: (Provider(), model, True))
    monkeypatch.setattr(system_prompt_builder, 'build_system_prompt', lambda **k: 'base prompt')
    engine, _ = devop_agents.create_devop_engine(
        tmp_path, tmp_path, agent={'id': 'AG-1', 'name': 'impl'}, role='implementer', operation_id='op-1')
    return engine


def test_devop_engine_withholds_clarify_through_the_interactive_flag(tmp_path, monkeypatch):
    from charon.conversation.task_policy import NON_INTERACTIVE_GUIDANCE

    monkeypatch.delenv('CHARON_TASK_TIME_BUDGET_SECONDS', raising=False)
    engine = _devop_engine(tmp_path, monkeypatch)
    assert engine.interactive is False
    assert 'Clarify' not in {t['name'] for t in engine.tools}
    assert 'Clarify' not in {t['name'] for t in engine._available_tools}
    prompt = engine._get_system_prompt()
    assert NON_INTERACTIVE_GUIDANCE in prompt
    # No per-pass wall-clock cap, as before the change.
    assert 'Task time budget' not in prompt


def test_devop_time_budget_still_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_TASK_TIME_BUDGET_SECONDS', '900')
    engine = _devop_engine(tmp_path, monkeypatch)
    assert 'Task time budget: 900 seconds' in engine._get_system_prompt()
