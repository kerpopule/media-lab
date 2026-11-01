import io
import math
import struct
import wave
import pytest
from media_lab_core.speech_artifact import inspect_wav, MAX_BYTES


def wav(*, rate=24000, channels=1, width=2, frames=2400, silent=False):
    out=io.BytesIO()
    with wave.open(out,'wb') as stream:
        stream.setparams((channels,width,rate,frames,'NONE','not compressed'))
        pcm=b''.join(struct.pack('<h',0 if silent else int(12000*math.sin(i*0.12))) for i in range(frames*channels))
        stream.writeframes(pcm if width==2 else b'\0'*(frames*channels*width))
    return out.getvalue()


def test_decoded_format_signal_and_identity():
    data=wav();result=inspect_wav(data)
    assert result['frames']==2400 and result['durationSeconds']==0.1
    assert result['sampleRate']==24000 and result['mimeType']=='audio/wav'
    assert result['bytes']==len(data) and len(result['sha256'])==64
    assert result['peak']>10000 and result['rms']>8000


@pytest.mark.parametrize('options',[{'rate':16000},{'channels':2},{'width':1},{'silent':True},{'frames':1}])
def test_invalid_contract_or_silent_output(options):
    with pytest.raises(ValueError):inspect_wav(wav(**options))


def test_truncation_and_container_length():
    data=wav()
    with pytest.raises(ValueError):inspect_wav(data[:-2])
    with pytest.raises(ValueError):inspect_wav(data+b'extra')
    broken=bytearray(data)
    struct.pack_into('<I',broken,40,len(data)-44+2)
    with pytest.raises(ValueError,match='incomplete'):inspect_wav(bytes(broken))


def test_bounded_input():
    with pytest.raises(ValueError):inspect_wav(b'\0'*(MAX_BYTES+1))
    with pytest.raises(ValueError):inspect_wav(bytearray(wav()))


def test_duplicate_audio_chunk_is_not_ignored():
    data=bytearray(wav())
    data.extend(b'data'+struct.pack('<I',2)+b'\0\0')
    struct.pack_into('<I',data,4,len(data)-8)
    with pytest.raises(ValueError,match='exactly one'):inspect_wav(bytes(data))
