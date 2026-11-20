import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx

from radar.extraction import extract_event
from radar.llm import _completion_url, enrich_with_model


class ModelExtractionTests(unittest.TestCase):
    title = "运输服务询价"
    text = "采购人：南方材料有限公司\n报价截止：2030-01-01\n报价方式：发送至rfq@example.com"
    config = {"LEAD_MODEL_ENABLED": "true", "LEAD_MODEL_BASE_URL": "https://example.com/v1", "LEAD_MODEL_NAME": "model-test", "LEAD_MODEL_API_KEY": "top-secret-do-not-log"}

    def model_output(self):
        return {"buyer_name": "南方材料有限公司", "buyer_role": "unknown", "service_types": ["transport"], "regions": [], "event_type": "rfq", "deadline": "2030-01-01", "deadline_kind": "bid", "deadline_has_time": False, "service_period": None, "qualifications": [], "participation_path": "发送至rfq@example.com", "evidence": [{"field": "buyer_name", "quote": "采购人：南方材料有限公司"}, {"field": "service_types", "quote": self.title}, {"field": "event_type", "quote": self.title}, {"field": "deadline", "quote": "报价截止：2030-01-01"}, {"field": "participation_path", "quote": "报价方式：发送至rfq@example.com"}]}

    def invoke(self, output, title=None, text=None, original=None):
        title, text = title or self.title, text or self.text
        original = original or extract_event(title, text)
        response = httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})
        with patch.dict(os.environ, self.config, clear=False), patch("radar.llm.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = response
            result = enrich_with_model(original, title, text)
            self.assertEqual(client.return_value.__enter__.return_value.post.call_count, 1)
            return result

    def test_disabled_never_calls_network(self):
        event = extract_event(self.title, self.text)
        with patch.dict(os.environ, {"LEAD_MODEL_ENABLED": "false"}), patch("radar.llm.httpx.Client") as client:
            result = enrich_with_model(event, self.title, self.text)
            client.assert_not_called()
        self.assertEqual(result, event)

    def test_valid_output_is_grounded_and_keeps_unknown_ownership(self):
        result = self.invoke(self.model_output())
        self.assertEqual(result["extraction_method"], "model")
        self.assertEqual(result["buyer_name"], "南方材料有限公司")
        self.assertEqual(result["ownership_status"], "unknown")
        self.assertEqual(result["processing_status"], "actionable")

    def test_missing_key_does_not_call_network(self):
        with patch.dict(os.environ, {**self.config, "LEAD_MODEL_API_KEY": ""}), patch("radar.llm.httpx.Client") as client:
            result = enrich_with_model(extract_event(self.title, self.text), self.title, self.text)
            client.assert_not_called()
        self.assertIn("配置不完整", result["model_warning"])

    def test_fabricated_quote_is_rejected(self):
        output = self.model_output()
        output["evidence"][0]["quote"] = "采购人：日本大企业有限公司"
        result = self.invoke(output)
        self.assertEqual(result["extraction_method"], "rules")
        self.assertIn("证据校验", result["model_warning"])

    def test_fabricated_buyer_with_real_quote_is_rejected(self):
        output = self.model_output()
        output["buyer_name"] = "日本大企业有限公司"
        result = self.invoke(output)
        self.assertEqual(result["buyer_name"], "南方材料有限公司")
        self.assertEqual(result["extraction_method"], "rules")

    def test_invented_midnight_is_rejected(self):
        output = self.model_output()
        output["deadline"] = "2030-01-01T23:59:00+08:00"
        output["deadline_has_time"] = True
        result = self.invoke(output)
        self.assertEqual(result["deadline"], "2030-01-01")
        self.assertEqual(result["extraction_method"], "rules")

    def test_publication_date_not_accepted_as_bid_deadline(self):
        text = self.text + "\n发布日期：2029-12-01"
        output = self.model_output()
        output["deadline"] = "2029-12-01"
        output["evidence"][3]["quote"] = "发布日期：2029-12-01"
        result = self.invoke(output, text=text)
        self.assertEqual(result["extraction_method"], "rules")
        self.assertEqual(result["deadline"], "2030-01-01")

    def test_foreign_ownership_and_actionability_extra_fields_rejected(self):
        for field, value in (("ownership_status", "japan_confirmed"), ("processing_status", "actionable")):
            with self.subTest(field=field):
                output = self.model_output()
                output[field] = value
                result = self.invoke(output)
                self.assertEqual(result["extraction_method"], "rules")

    def test_model_cannot_promote_expansion_signal(self):
        event = extract_event(self.title, self.text, source_kind="early_signal")
        result = self.invoke(self.model_output(), original=event)
        self.assertEqual(result["processing_status"], "early_signal")

    def test_unknown_fields_do_not_erase_existing_facts(self):
        output = self.model_output()
        output["buyer_name"] = None
        output["participation_path"] = None
        result = self.invoke(output)
        self.assertEqual(result["buyer_name"], "南方材料有限公司")
        self.assertEqual(result["participation_path"], "发送至rfq@example.com")

    def test_http_error_does_not_leak_secret_or_response(self):
        event = extract_event(self.title, self.text)
        with patch.dict(os.environ, self.config), patch("radar.llm.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = httpx.Response(401, text="secret-provider-body top-secret-do-not-log")
            result = enrich_with_model(event, self.title, self.text)
        self.assertIn("HTTP 401", result["model_warning"])
        self.assertNotIn("top-secret", str(result))
        self.assertNotIn("provider-body", str(result))

    def test_invalid_json_safe_fallback(self):
        with patch.dict(os.environ, self.config), patch("radar.llm.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
            result = enrich_with_model(extract_event(self.title, self.text), self.title, self.text)
        self.assertEqual(result["extraction_method"], "rules")
        self.assertIn("校验", result["model_warning"])

    def test_endpoint_validation(self):
        self.assertEqual(_completion_url("https://example.com/v1/"), "https://example.com/v1/chat/completions")
        self.assertEqual(_completion_url("https://example.com/v1/chat/completions"), "https://example.com/v1/chat/completions")
        self.assertEqual(_completion_url("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/v1/chat/completions")
        for endpoint in ("http://example.com/v1", "https://key:secret@example.com/v1", "https://example.com/v1?api_key=secret", "file:///tmp/model"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                _completion_url(endpoint)


if __name__ == "__main__":
    unittest.main()
