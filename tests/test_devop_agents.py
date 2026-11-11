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
