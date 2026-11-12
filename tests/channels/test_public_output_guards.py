"""动作身份和播放证据不能由宽松truthy值或矛盾计数组成。"""
import pytest

from channels.models import OutputFrame, PlaybackResult, RoomTarget, SessionRef, SetOutput


def session():
    return SessionRef('run','binding','room',RoomTarget('a','b'))


@pytest.mark.parametrize('identifier',[True,42,['action'],'  '])
def test_output_action_identity_must_be_a_nonempty_string(identifier):
    with pytest.raises(ValueError):
        SetOutput(identifier,session(),volume=50)
    with pytest.raises(ValueError):
        OutputFrame(identifier,session(),'output',0,b'\0\1',24000)


@pytest.mark.parametrize('change',[
    {'confirmed_samples':0,'unknown_samples':1},
    {'submitted_samples':True}, {'submitted_frames':True}, {'submitted_samples':-1},
    {'confirmed_samples':2}, {'unknown_samples':1},
])
def test_completed_playback_cannot_claim_unconfirmed_or_inconsistent_audio(change):
    values=dict(action_id='action',session=session(),output_id='out',status='completed',
                submitted_frames=1,submitted_samples=1,confirmed_samples=1,unknown_samples=0)
    values.update(change)
    with pytest.raises(ValueError):
        PlaybackResult(**values)


@pytest.mark.parametrize('values',[{'sample_rate':1},{'sample_rate':96001},{'frame_ms':16000,'max_utterance_ms':15000}])
def test_vad_configuration_cannot_leave_input_without_a_viable_cut_budget(values):
    from channels.models import VadvConfig
    with pytest.raises(ValueError):
        VadvConfig(**values)
