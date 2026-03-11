from pathlib import Path

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from subprocess import CalledProcessError, run
from typing import Optional


IMESSAGE_SCRIPT = (Path(__file__).resolve().parent / "send-imessage.scpt").as_posix()
API_TOKEN = "change-me-imessage-token"


class IMessageRequest(BaseModel):
    to: str
    text: str


app = FastAPI()


@app.post("/send-imessage")
def send_imessage(payload: IMessageRequest, x_api_token: Optional[str] = Header(None)):
    if x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        result = run(
            ["osascript", IMESSAGE_SCRIPT, payload.to, payload.text],
            capture_output=True,
            text=True,
            check=True,
        )
        return {"ok": True, "stdout": result.stdout.strip()}
    except CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to send iMessage", "stderr": e.stderr},
        )
