import io, os, sys, time, unittest
from unittest.mock import patch
os.environ["AUTH_TOKEN"] = "offline-test-token"
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
AUTH = {"Authorization": "Bearer offline-test-token"}
PDF = b"%PDF-1.7\n" + b"x" * (60 * 1024)
URL = "https://patentimages.storage.googleapis.com/test/patent.pdf"

class FakeResponse:
    def __init__(self, data=b"", status=200, url=URL, headers=None):
        self.data, self.status_code, self.url = data, status, url
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.closed = False
    def iter_content(self, chunk_size):
        for i in range(0, len(self.data), chunk_size):
            yield self.data[i:i+chunk_size]
    def close(self): self.closed=True
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.client=s.app.test_client()
        self.deadline=time.monotonic()+100
    def test_auth_and_invalid_body(self):
        self.assertEqual(self.client.get("/health").json, {"status":"ok"})
        for token in ("", "Bearer wrong", "Bearer 非ASCII"):
            with self.subTest(token=token):
                self.assertEqual(self.client.post("/download",json={},headers={"Authorization":token}).status_code,401)
        for body in ([], ["CN111877788A"], "x", 123, None, {}, {"patent_number":1},
                     {"patent_number":[]}, {"patent_number":"../secret"},
                     {"patent_number":"CN123?x=1"}, {"patent_number":"CN123\r\nX:A"},
                     {"patent_number":"ＡＢ123"}, {"patent_number":"C"*101}):
            with self.subTest(body=body):
                r=self.client.post("/download",json=body,headers=AUTH)
                self.assertEqual(r.status_code,400)
                self.assertEqual(r.json["code"],"invalid_request")
        r=self.client.post("/download",data="{broken",content_type="application/json",headers=AUTH)
        self.assertEqual(r.status_code,400)
        r=self.client.post("/download",data='{"patent_number":"'+("A"*20000)+'"}',content_type="application/json",headers=AUTH)
        self.assertEqual(r.status_code,413)
    def test_pdf_stream_slot_and_headers(self):
        fp=io.BytesIO(PDF)
        with patch.object(s,"fetch_pdf",return_value=(fp,len(PDF))) as fetch:
            response=self.client.post("/download",json={"patent_number":" cn 111877788a "},headers=AUTH,buffered=False)
            fetch.assert_called_once_with("CN111877788A")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers["X-Patent-Number"],"CN111877788A")
            self.assertEqual(response.headers["Cache-Control"],"no-store")
            self.assertEqual(response.content_length,len(PDF))
            self.assertTrue(s.SLOTS.acquire(False))
            self.assertFalse(s.SLOTS.acquire(False))
            s.SLOTS.release()
            self.assertEqual(response.data,PDF)
            response.close()
            self.assertTrue(fp.closed)
        self.assertTrue(s.SLOTS.acquire(False)); self.assertTrue(s.SLOTS.acquire(False))
        s.SLOTS.release(); s.SLOTS.release()
    def test_busy_does_not_block_health(self):
        s.SLOTS.acquire(); s.SLOTS.acquire()
        try:
            r=self.client.post("/download",json={"patent_number":"CN111877788A"},headers=AUTH)
            self.assertEqual(r.status_code,429); self.assertEqual(r.headers["Retry-After"],"5")
            self.assertEqual(self.client.get("/health").status_code,200)
        finally:
            s.SLOTS.release(); s.SLOTS.release()
    def test_error_releases_slot(self):
        with patch.object(s,"fetch_pdf",side_effect=s.FetchError("not found","not_found")):
            r=self.client.post("/download",json={"patent_number":"US123A"},headers=AUTH)
            self.assertEqual(r.status_code,502); self.assertEqual(r.json["code"],"not_found")
        self.assertTrue(s.SLOTS.acquire(False)); self.assertTrue(s.SLOTS.acquire(False))
        s.SLOTS.release(); s.SLOTS.release()
    def test_redirect_rejected_before_request(self):
        response=FakeResponse(status=302,headers={"Location":"http://169.254.169.254/latest/meta-data"})
        with patch.object(s,"_session") as session:
            session.return_value.get.return_value=response
            with self.assertRaises(s.FetchError): s._open(URL,{"patentimages.storage.googleapis.com"},self.deadline)
            self.assertEqual(session.return_value.get.call_count,1)
            self.assertTrue(response.closed)
        for url in ("http://patentimages.storage.googleapis.com/x","https://evil.example/x",
                    "https://user:pass@patentimages.storage.googleapis.com/x",
                    "https://patentimages.storage.googleapis.com:8443/x",
                    "https://patentimages.storage.googleapis.com.evil.example/x"):
            self.assertFalse(s._valid_upstream(url,{"patentimages.storage.googleapis.com"}))
    def test_limits_and_deadline(self):
        with self.assertRaises(s.FetchError) as ctx:
            s._copy_limited(FakeResponse(b"x"*30),io.BytesIO(),20,self.deadline)
        self.assertEqual(ctx.exception.code,"too_large")
        with self.assertRaises(s.FetchError) as ctx:
            s._copy_limited(FakeResponse(b"x"),io.BytesIO(),20,time.monotonic()-1)
        self.assertEqual(ctx.exception.code,"timeout")
    def test_parsers_and_upstream_codes(self):
        self.assertFalse(s._is_blocked_page("A patent about captcha recognition"))
        self.assertTrue(s._is_blocked_page("sending automated queries"))
        page='<meta name="citation_pdf_url" content="'+URL+'">'
        self.assertEqual(s._extract_pdf_url(page),URL)
        for status,body,code in ((404,"not found","not_found"),(200,"no pdf","no_pdf"),
                                  (503,"unusual traffic","denied"),(503,"busy","blocked")):
            with patch.object(s,"_get_page",return_value=(status,body,"https://patents.google.com/test")):
                with self.assertRaises(s.FetchError) as ctx: s.fetch_pdf("CN123A")
                self.assertEqual(ctx.exception.code,code)
        with patch.object(s,"_get_page",return_value=(200,page,"https://patents.google.com/test")):
            with patch.object(s,"_open",return_value=FakeResponse(PDF)):
                fp,size=s.fetch_pdf("CN123A")
                self.assertEqual(size,len(PDF)); self.assertEqual(fp.read(),PDF); fp.close()
            with patch.object(s,"_open",return_value=FakeResponse(b"<html>"+b"x"*60000)):
                with self.assertRaises(s.FetchError): s.fetch_pdf("CN123A")
    def test_spool_rollover(self):
        with s.SpooledTemporaryFile(max_size=10,mode="w+b") as fp:
            size=s._copy_limited(FakeResponse(b"x"*30),fp,100,self.deadline)
            self.assertEqual(size,30); self.assertTrue(fp._rolled)
if __name__=="__main__":
    unittest.main(verbosity=2)
