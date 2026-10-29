"""A tiny OpenAI-compatible server for tests. It answers from a prepared 'good' analysis and can misbehave on purpose."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockLLM:
    def __init__(self, good, mode="good"):
        self.good, self.mode, self.calls, self.bodies = good, mode, [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.bodies.append(body)
                msgs = body["messages"]
                users = [m for m in msgs if m["role"] == "user"]
                first = users[0]["content"]
                has_image = not isinstance(first, str)
                first = first if isinstance(first, str) else first[0]["text"]
                repair = any("The checks found these problems" in (m["content"] if isinstance(m["content"], str) else "") for m in users[1:])
                outer.calls.append({"first": first[:60], "repair": repair, "has_image": has_image, "json_mode": "response_format" in body,
                                    "auth": self.headers.get("Authorization", "")})
                if outer.mode == "down":
                    return self._send(503, {"error": "down"})
                if outer.mode == "bad_key":
                    return self._send(401, {"error": {"message": "invalid api key"}})
                if outer.mode == "rate_limit_once" and len(outer.calls) == 1:
                    return self._send(429, {"error": "slow down"}, {"Retry-After": "0"})
                if outer.mode == "refuse_image" and has_image:
                    return self._send(400, {"error": {"message": "This image cannot be processed (content policy)."}})
                if outer.mode == "new_token_name" and "max_tokens" in body:
                    return self._send(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
                return self._send(200, {"choices": [{"message": {"role": "assistant", "content": outer.answer(first, repair, has_image)}, "finish_reason": "stop"}]})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def answer(self, prompt, repair, has_image=False):
        if "single word OK" in prompt:
            return "OK"
        if "What colour" in prompt:
            return "red"
        if "STEP 1 OF 2" in prompt:
            name = re.search(r"- Template: (\S+)", prompt).group(1)
            t = next((x for x in self.good["templates"] if x["template"] == name), self.good["templates"][0])
            frag = {k: t[k] for k in ("content_risk", "content_risk_basis", "works", "watch_outs")}
            frag["variations"] = [{"name": "Variation 1 - test", "title": "T", "description": ["D"], "why": "W"}]
            if self.mode == "other_brand":
                frag["watch_outs"] = frag["watch_outs"] + [{"h": "Compare", "b": "Axis does this better."}]
            if self.mode == "flip" and name == "demo_code_100":
                frag["content_risk"] = "High"
            out = json.dumps(frag)
            if self.mode == "fenced":
                return "Sure, here is the review:\n```json\n" + out + "\n```\nHope that helps."
            if self.mode == "think":
                return "<think>Let me look at this card carefully. {not json}</think>\n" + out
            return out
        g = {k: v for k, v in self.good.items() if k not in ("templates", "meta", "feature_matrix", "reference_templates")}
        if self.mode == "bad_number_once" and not repair:
            g = json.loads(json.dumps(g))
            g["summary"]["bullets"][1] = re.sub(r"(\d+)\.(\d+)%", lambda m: f"{m.group(1)}.{(int(m.group(2)) + 3) % 100:02d}%", g["summary"]["bullets"][1], count=1)
        return json.dumps(g)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
