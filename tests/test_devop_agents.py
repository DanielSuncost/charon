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
