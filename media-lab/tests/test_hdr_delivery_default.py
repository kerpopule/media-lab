"""Disposable real-codec tests. Run directly, no app import or shared bootstrap."""
import ast
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from media_lab_core import hdr_delivery as d

class HDRDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='delivery-fixture-')
        self.root=Path(self.tmp.name)
        self.source=self.root/"source ' safe; SDR.mp4"
        subprocess.run(['ffmpeg','-v','error','-n','-f','lavfi','-i','testsrc2=s=128x72:r=24:d=0.5',
            '-f','lavfi','-i','sine=frequency=440:duration=0.5','-c:v','libx264','-pix_fmt','yuv420p',
            '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','tv','-c:a','aac',str(self.source)],check=True,capture_output=True)
    def tearDown(self): self.tmp.cleanup()
    def test_real_av_and_original_preserved(self):
        target=self.root/'out.mp4';before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        r=d.prepare(self.source,target,self.root/'job')
        self.assertEqual(r['codec_vui']['transfer_characteristics'],[18])
        self.assertEqual(r['container_colr'][0]['values'],[9,18,9])
        self.assertEqual(r['policy'],d.POLICY)
        self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(before,hashlib.sha256((self.root/'job/engine-original.mp4').read_bytes()).hexdigest())
        self.assertTrue(any(s['codec_type']=='audio' for s in r['probe']['streams']))
    def test_already_hlg_no_second_grade(self):
        first=self.root/'first.mp4';second=self.root/'second.mp4'
        d.encode(self.source,first,assume_sdr709=True)
        with patch.object(d.hdr_finish,'finish',side_effect=AssertionError('double grade')):
            r=d.encode(first,second)
        self.assertIn('no second grade',r['source_transform'])
    def test_cancel_before_encoding(self):
        target=self.root/'cancel.mp4'
        with self.assertRaisesRegex(RuntimeError,'cancelled'):d.encode(self.source,target,cancelled=lambda:True)
        self.assertFalse(target.exists())
    def test_refuse_existing_output(self):
        with self.assertRaisesRegex(ValueError,'overwrite'):d.encode(self.source,self.source)
    def test_no_sdr_fallback_on_encoder_failure(self):
        with patch.object(d.hdr_finish,'finish',side_effect=RuntimeError('codec failed')):
            with self.assertRaisesRegex(RuntimeError,'codec failed'):d.encode(self.source,self.root/'failed.mp4')
        self.assertFalse((self.root/'failed.mp4').exists())
    def test_pq_refused(self):
        info=d.hdr_finish.probe(self.source);info['streams'][0]['color_transfer']='smpte2084'
        with patch.object(d.hdr_finish,'probe',return_value=info):
            with self.assertRaisesRegex(ValueError,'PQ source'):d.encode(self.source,self.root/'pq.mp4')
    def test_foreign_gamut_even_with_assumption_refused(self):
        info=d.hdr_finish.probe(self.source);info['streams'][0]['color_primaries']='smpte432'
        with patch.object(d.hdr_finish,'probe',return_value=info):
            with self.assertRaisesRegex(ValueError,'Incompatible'):d.encode(self.source,self.root/'p3.mp4',assume_sdr709=True)
    def test_real_finalizer_hook(self):
        import os, shutil
        text=(Path(__file__).resolve().parents[1]/'app.py').read_text()
        fn=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name=='_finish_video')
        events=[]
        def failed(*args):raise AssertionError(args)
        scope={'Path':Path,'MEDIA':self.root,'JOBS_DIR':self.root/'jobs','subprocess':subprocess,
               'os':os,'shutil':shutil,'save_state':lambda:None,'fail':failed,
               '_maybe_face_fix':lambda j,p:None,'gallery_add':lambda *a,**kw:events.append(a)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<exact-finalizer>','exec'),scope)
        j={'id':'fixture','prompt':'Synthetic fixture'}
        scope['_finish_video'](j,self.source)
        self.assertEqual(j['status'],'done')
        self.assertEqual(len(events),1)
        d.hdr_finish.verify(self.root/'fixture.mp4','x264')
        self.assertEqual(j['hdr_delivery']['policy'],d.POLICY)
    def test_library_gate_preserves_valid_hlg_bytes(self):
        target=self.root/'already.mp4';d.encode(self.source,target,assume_sdr709=True)
        before=target.read_bytes()
        d.enforce_library_video(target,self.root/'library')
        self.assertEqual(target.read_bytes(),before)
    def test_library_gate_old_stitch_and_enhancement(self):
        import json
        text=(Path(__file__).resolve().parents[1]/'app.py').read_text()
        fn=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name=='gallery_add')
        saved=[]
        scope={'Path':Path,'MEDIA':self.root,'JOBS_DIR':self.root/'jobs','ROOT':self.root,
               'jobs':{},'time':__import__('time'),'_load':lambda *a:[],
               '_save':lambda p,g:saved.append(g),'_model_annotate':lambda row,job:row,
               'media_path':lambda url:(self.root/Path(url).name) if url else None}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<exact-gallery>','exec'),scope)
        scope['gallery_add']('fixture','Synthetic old stitch','boardfilm','/media/'+self.source.name,'/media/preview.jpg')
        self.assertEqual(len(saved),1)
        self.assertTrue((self.root/'preview.jpg').is_file())
        d.hdr_finish.verify(self.source,'x264')
    def test_library_gate_rejects_lost_hdr_tags(self):
        original=self.root/'hdr-original.mp4';d.encode(self.source,original,assume_sdr709=True)
        with self.assertRaisesRegex(ValueError,'lost HDR source'):
            d.enforce_library_video(self.source,self.root/'unsafe',input_source=original)
    def test_hook_default_and_failure_no_copy_fallback(self):
        text=(Path(__file__).resolve().parents[1]/'app.py').read_text()
        fn=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name=='_finish_video')
        source=ast.get_source_segment(text,fn)
        assert source is not None
        self.assertIn('prepare_hdr(',source)
        self.assertNotIn('shutil.copy2(out, temp)',source)
        self.assertNotIn('_maybe_face_fix(j, final)',source)
        self.assertIn('before HDR publication',source)
if __name__=='__main__':unittest.main(verbosity=2)
