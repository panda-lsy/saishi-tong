"""Delegated reviews must be explicit metadata, never invented AI evidence."""
import tempfile
from pathlib import Path
import unittest
from saishi_tong import store


class ReviewNoteTests(unittest.TestCase):
    def test_explicit_review_note_is_retained_without_inventing_ai(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'test.db'
            initial=store.save_admin_contest({'contest_name':'示例征文大赛','source_url':'https://example.test/event'},path=path)
            note='用户明确授权助手校对；已核对官方来源，待用户复审。'
            result=store.save_admin_contest({'human_reviewed':True,'review_note':note,'expected_fingerprint':initial['record_fingerprint']},initial['id'],path=path)
            self.assertTrue(result['human_reviewed'])
            self.assertIn('授权校对说明：'+note,result['verification_notes'])
            self.assertEqual(result['luna_model'],'');self.assertEqual(result['luna_verified_at'],'')
            self.assertFalse(result['ai_reviewed_at'])
            with self.assertRaises(ValueError):store.save_admin_contest({'review_note':'不可脱离确认单独写入'},initial['id'],path=path)
