from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text()
JS = (ROOT / "frontend" / "app.js").read_text()


def test_restore_control_is_contextual_not_global_undo():
    organize = HTML.split('class="organize-pane"', 1)[1].split('class="inspect-pane"', 1)[0]
    inspect = HTML.split('class="inspect-pane"', 1)[1].split('class="page-stepper"', 1)[0]
    assert 'id="undo-delete"' not in organize
    assert inspect.index('id="delete"') < inspect.index('id="undo-delete"')
    assert 'Restore deleted page' in inspect
    assert inspect.index('id="undo-delete"') < inspect.index('id="reset-edits"')
    assert 'id="deleted-page-count"' in inspect


def test_restore_does_not_affect_general_edit_undo():
    assert "state.job.deleted_pages.at(-1)?.page?.id" in JS
    assert 'Deleted page restored to its previous position.' in JS
    assert "$('#undo-delete').disabled=deletedCount===0" in JS
    assert 'Page removed. You can undo it.' not in JS



def test_zoom_pan_and_crop_handles_are_discoverable():
    for elem in ['id="large-pan-toggle"', 'id="zoom-fit"', 'id="crop-pan-toggle"',
                 'id="crop-zoom-in"', 'id="crop-zoom-out"', 'id="crop-zoom-fit"']:
        assert elem in HTML
    for side in ('nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'):
        assert f'data-handle="{side}"' in HTML
    assert 'wirePanViewport' in JS
    assert 'moveCropSelection' in JS
    assert 'beginCropSelection' in JS
