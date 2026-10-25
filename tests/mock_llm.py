"""A tiny OpenAI-compatible server for tests. It answers from a prepared 'good' analysis and can misbehave on purpose."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockLLM:
    def __init__(self, good, mode="good"):
        self.good, self.mode, self.calls, self.fail_templates = good, mode, [], set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                msgs = body["messages"]
                first = msgs[1]["content"]
                first = first if isinstance(first, str) else first[0]["text"]
                repair = any("The checks found these problems" in (m["content"] if isinstance(m["content"], str) else "") for m in msgs)
                outer.calls.append({"first": first[:60], "repair": repair, "has_image": not isinstance(msgs[1]["content"], str), "json_mode": "response_format" in body})
                if outer.mode == "down":
                    self.send_response(503); self.end_headers(); return
                content = outer.answer(first, repair)
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def answer(self, prompt, repair):
        if "STEP 1 OF 2" in prompt:
            name = re.search(r"- Template: (\S+)", prompt).group(1)
            t = next(x for x in self.good["templates"] if x["template"] == name)
            frag = {k: t[k] for k in ("content_risk", "content_risk_basis", "works", "watch_outs")}
            frag["variations"] = [{"name": "Variation 1 - test", "title": "T", "description": ["D"], "why": "W"}]
            if self.mode == "other_brand":
                frag["watch_outs"] = frag["watch_outs"] + [{"h": "Compare", "b": "Axis does this better."}]
            if self.mode == "flip" and name == "demo_code_100":
                frag["content_risk"] = "High"
            return ("Sure, here is the review:\n```json\n" + json.dumps(frag) + "\n```\nHope that helps.") if self.mode == "fenced" else json.dumps(frag)
        g = {k: v for k, v in self.good.items() if k not in ("templates", "meta", "feature_matrix", "reference_templates")}
        if self.mode == "bad_number_once" and not repair:
            g = json.loads(json.dumps(g))
            g["summary"]["bullets"][1] = re.sub(r"(\d+)\.(\d+)%", lambda m: f"{m.group(1)}.{(int(m.group(2)) + 3) % 100:02d}%", g["summary"]["bullets"][1], count=1)
        return json.dumps(g)

    def __enter__(self):
        self.t.start(); return self

    def __exit__(self, *a):
        self.httpd.shutdown()
