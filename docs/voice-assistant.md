# In-App Voice Assistant

이 문서는 MobileWorld 위에 만든 **음성 비서**를 다룹니다. 이 비서는 **내 앱 하나 안에서만** 동작합니다.
사용자가 말하면 에이전트가 앱 기능(툴)을 호출하거나 화면을 직접 조작하고, 결과를 음성으로 알려줍니다.

```
 🎤 STT ──► VoiceAssistantSession ──► InAppAssistantAgent (LLM)
              │        ▲                    │ 
              │        └── screenshot / tool result / guard note
              ▼
        AppScopeGuard ──► 앱 툴 (deeplink / broadcast / python)   ← 툴이 있으면 우선
              │       └─► GUI 액션 (click, scroll, input_text …)    ← 없으면 화면 조작
              ▼
        DeviceBackend (지금: MobileWorld server + adb / 나중: 앱 내부)
 🔊 TTS ◄── answer / ask_user
```

## 구성 요소

| 모듈 | 역할 |
|---|---|
| [`assistant/speech.py`](../src/mobile_world/assistant/speech.py) | `SpeechToText` / `TextToSpeech` 인터페이스. 엔진은 `console`(키보드), `whisper`(faster-whisper, 로컬), `openai`(OpenAI 호환 API) |
| [`assistant/device.py`](../src/mobile_world/assistant/device.py) | `DeviceBackend` 인터페이스와 `MobileWorldDevice`(ADB 구현) |
| [`assistant/app_tools.py`](../src/mobile_world/assistant/app_tools.py) | 앱 기능을 툴로 등록. `deeplink`, `broadcast`, Python 함수 |
| [`assistant/scope.py`](../src/mobile_world/assistant/scope.py) | `AppScopeGuard`: 홈 버튼과 다른 앱 실행을 차단하고, 앱을 벗어나면 되돌아옴 |
| [`assistant/agent.py`](../src/mobile_world/assistant/agent.py) | `InAppAssistantAgent`: `general_e2e`를 확장해 툴 우선, 음성 답변, 확인 질문 규칙을 넣은 에이전트 |
| [`assistant/neulbab/`](../src/mobile_world/assistant/neulbab/) | 늘밥 툴 계약을 구현한 모의 앱(웹 로직 이식), 역할별 툴과 말투 지침 |
| [`assistant/webapp/`](../src/mobile_world/assistant/webapp/) | 휴대폰 웹앱: 서버(FastAPI, WebSocket), OpenAI 음성, 화면(HTML/JS) |
| [`assistant/session.py`](../src/mobile_world/assistant/session.py) | 듣기 → 실행 → 말하기 루프. 이전 대화 문맥을 기억하고, 취소와 종료 명령을 처리 |

### 동작 규칙
- **툴 우선**: 툴로 처리할 수 있는 요청은 GUI를 거치지 않고 툴로 바로 처리합니다.
- **앱 이탈 방지**: `navigate_home`, 다른 앱의 `open_app`, 등록되지 않은 툴 호출은 실행 전에 차단합니다. 차단한 이유는 에이전트에게 다시 전달됩니다.
  링크 등을 눌러 다른 앱이 열리면 먼저 뒤로가기를 누르고, 그래도 돌아오지 않으면 앱을 다시 실행합니다.
- **되돌릴 수 없는 동작은 확인**: 전송, 결제, 삭제 같은 동작은 먼저 `ask_user`로 "할까요?"라고 물어봅니다.
- **음성 친화적 답변**: 1~2문장으로 짧게, 마크다운이나 좌표 없이 답합니다.
- **음성 명령**: "취소"라고 하면 진행 중인 요청을 중단하고, "종료"라고 하면 비서를 끕니다.

## 실행

```bash
uv sync
# 마이크와 스피커를 쓸 경우 (선택)
uv pip install sounddevice faster-whisper

# 1) 텍스트로 한 번만 실행 (마이크 없이 개발할 때)
uv run mw assistant \
  --package com.example.myapp --app-name 마이앱 \
  --model-name anthropic/claude-sonnet-4-5 --llm-base-url https://openrouter.ai/api/v1 \
  --api-key $API_KEY \
  --tools-file docs/assistant_tools.example.json \
  --once "최근 주문 내역 알려줘"

# 2) 음성 대화 모드: 로컬 Whisper STT + OpenAI TTS
uv run mw assistant \
  --package com.example.myapp --app-name 마이앱 \
  --model-name <model> --llm-base-url <url> \
  --stt whisper --stt-model small --tts openai --speech-api-key $OPENAI_API_KEY
```

- `--aw-host`를 생략하면 `mw test`와 똑같이 MobileWorld 서버를 자동으로 띄우고, ADB Keyboard 설치 여부도 확인합니다.
- `--extra-package`로 비서가 머물러도 되는 패키지를 추가할 수 있습니다. 예를 들어 앱이 사용하는 결제 SDK 화면이 해당됩니다.
- `--language en`으로 바꾸면 영어로 대화합니다.

## 앱 툴 정의하기

툴은 JSON 파일로 정의합니다. 예시는 [`assistant_tools.example.json`](assistant_tools.example.json)에 있습니다.

| type | 호출 방식 | 앱 쪽 구현 |
|---|---|---|
| `deeplink` | `am start -a VIEW -d myapp://orders/{order_id} -p <pkg>` | 해당 URI를 받는 `intent-filter` |
| `broadcast` | `am broadcast -a <action> -p <pkg> --es key value` | `BroadcastReceiver`를 두고 `setResultData(json)`로 결과를 돌려줌 |

broadcast 툴을 받는 앱 쪽 코드 예시입니다.

```kotlin
class AssistantReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            "com.example.myapp.assistant.GET_RECENT_ORDERS" -> {
                val limit = intent.getStringExtra("limit")?.toIntOrNull() ?: 5
                val orders = OrderRepository.recent(limit)          // 앱의 기존 로직 재사용
                setResultData(Json.encodeToString(orders))           // → 에이전트가 받는 툴 결과
            }
            "com.example.myapp.assistant.ADD_TO_CART" -> {
                Cart.add(intent.getStringExtra("product_id")!!, intent.getStringExtra("quantity")?.toInt() ?: 1)
                setResultData("""{"ok": true}""")
            }
        }
    }
}
```
```xml
<!-- 디버그 빌드에서만 노출하세요. exported receiver는 다른 앱도 호출할 수 있습니다. -->
<receiver android:name=".AssistantReceiver" android:exported="true">
    <intent-filter>
        <action android:name="com.example.myapp.assistant.GET_RECENT_ORDERS" />
        <action android:name="com.example.myapp.assistant.ADD_TO_CART" />
    </intent-filter>
</receiver>
```

Python 코드에서 직접 툴을 등록할 수도 있습니다. 서버 API를 호출하거나 테스트용 mock을 만들 때 쓰면 됩니다.

```python
from mobile_world.assistant import ToolRegistry
tools = ToolRegistry()

@tools.tool("get_weather", "Get today's weather for a city",
            {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
def get_weather(city: str):
    return {"city": city, "temp_c": 21}
```

## 늘밥 모의 앱으로 실행 (폰 없이)

늘밥 Android 앱이 아직 없어서, [툴 계약](neulbab/tool_contract.md)을 그대로 구현한 **모의 앱**(`assistant/neulbab/`)을 두었습니다.
추천, 건강 필터, 주문 상태, 문구 등 웹 프로토타입(`mock_api.js`)의 규칙을 Python으로 옮긴 것입니다. 폰, 에뮬레이터, MobileWorld 서버 없이 툴만으로 대화합니다.

```bash
export NEULBAB_APP_DIR=/path/to/agent-company/product/app   # 메뉴·영양·건강 데이터를 여기서 읽음
uv run mw assistant --mock-neulbab --role parent \
  --model-name <model> --llm-base-url <openai-compatible-url> --api-key $API_KEY \
  --mock-state /tmp/neulbab_state.json        # 선택: 어르신 → 보호자 순서로 실행해도 주문이 이어짐
```

- `--role parent`(어르신) 또는 `--role guardian`(보호자). 역할마다 받는 툴과 말투 지침이 다릅니다.
- 화면이 없는 모드라 GUI 동작(탭, 스크롤 등)은 가드가 막고, 에이전트에게 툴만 쓰라고 알려줍니다.
- 데이터 파일은 이 저장소에 복사하지 않고 실행할 때 `NEULBAB_APP_DIR`에서 읽습니다.
- 웹과 같은 시연용 시간 간격(10초마다 접수 → 조리중 → 배달중 → 도착)을 씁니다.

**웹과 같은지 검증:** `tests/assistant/test_neulbab_parity.py`가 무작위 상황 300개를 원본 `mock_api.js`(Node)와 Python 양쪽에 넣고 결과를 비교합니다.
추천 순서, 건강 판정, 상태, 알림 문구, 조사 처리까지 모두 같아야 통과합니다. `NEULBAB_APP_DIR`과 `node`가 있을 때만 실행됩니다.

## 응답 속도를 줄이는 장치

음성 대화에서는 첫 반응이 1~2초 안에 와야 합니다. 세션에는 아래 장치가 있고, 늘밥 모의 실행에서는 기본으로 모두 켜져 있습니다.

| 장치 | 하는 일 | 끄는 옵션 |
|---|---|---|
| 규칙 처리 | "뭐 먹지", "언제 와", "취소해 줘" 같은 분명한 요청은 LLM 없이 처리 (취소는 확인 질문을 거침) | `--no-router` |
| 상황 미리 넣기 | 상황, 고를 수 있는 메뉴, 최근 주문을 요청에 붙여 조회용 LLM 호출을 없앰 | `--no-prefetch` |
| 세션이 처리하는 확인 | 앱이 만든 확인 질문을 세션이 읽고 예/아니요를 판별해 실행. LLM은 확인 번호를 보지 못함 | `--no-session-confirm` |
| 첫마디 | LLM이 N초 넘게 생각하면 "잠시만요." | `--filler-after -1` |
| 스트리밍 | 답변을 문장이 완성되는 대로 읽음 | `--no-stream` |
| 추론 강도 | GPT-5 같은 추론 모델의 생각 양 (`minimal`이 가장 빠름) | `--reasoning-effort` |

결과는 [neulbab/eval.md](neulbab/eval.md)의 "결과"를 보세요. GPT-5 `minimal`에서 과제당 19.8초가 2.2초로 줄었습니다.

## 휴대폰 웹앱 (안드로이드·아이폰)

휴대폰 브라우저에서 말로 쓰는 늘밥 비서입니다. 화면은 **늘밥 웹앱 원본 그대로**이고(처음 화면에서 보호자 또는 어르신으로 시작 → 그 화면 하나),
어르신·보호자 화면에 **마이크 버튼과 자막 말풍선만** 얹습니다. 원본 파일은 복사하지 않고 실행할 때 `NEULBAB_APP_DIR`에서 읽습니다.

- 위젯은 Shadow DOM 안에 있어서 늘밥 화면의 스타일과 서로 섞이지 않습니다.
- 늘밥 화면은 주문·설정을 브라우저 저장소(`neulbab_settings`, `neulbab_orders`)에 둡니다. 위젯이 이것을 서버의 가족 상태와 양쪽으로 맞춥니다.
  말로 주문하면 늘밥 화면이 저절로 바뀌고, 화면을 눌러 주문하면 음성 비서와 다른 가족 휴대폰에도 반영됩니다.
- `?code=접속번호`를 붙여 열면 번호를 기억합니다. 없으면 마이크를 처음 누를 때 묻습니다.
- `--standalone`으로 띄우면 늘밥 화면 없이 음성 전용 화면(`/voice/`)만 씁니다.

**어르신 화면은 손을 쓰지 않아도 됩니다.**
- 들어오면 "눌러서 시작하기"를 한 번 누릅니다. 브라우저는 누르기 전에는 소리를 내지 못하게 막기 때문에 한 번은 꼭 필요합니다.
- 그다음부터는 화면 안내를 자동으로 읽습니다. 예: "오늘 뭐 드실래요? 들깨미역국 정식, 가자미조림 정식, 고등어구이 정식이 있어요. 드시고 싶은 걸 말씀하시거나 그림을 눌러 주세요."
  다른 메뉴 화면, 확인 화면("이대로 주문할까요?"), 주문 상태(단계가 바뀔 때마다)도 읽습니다. 비서가 방금 말한 내용은 다시 읽지 않습니다.
- 안내가 끝나면 마이크가 저절로 켜져 기다리다가, 말을 시작하면 그 말만 보냅니다. 비서가 말하는 동안에는 자기 목소리를 듣지 않게 꺼 둡니다.
- 화면 자체의 질문(이대로 주문할까요? / 주문을 취소할까요?)에 "네"·"아니요"라고 하면 그 화면의 버튼을 대신 누릅니다.
- [말하기] 버튼을 누르면 자동 듣기를 끄고(버튼이 "켜기"로 바뀜), 다시 누르면 켭니다.
- 보호자 화면은 마이크 버튼을 눌러서 씁니다.
홈 화면에 추가하면 앱처럼 열립니다(안드로이드 Chrome의 "앱 설치", 아이폰 Safari의 "홈 화면에 추가").

```
휴대폰 ──WebSocket(https)──> 서버: 음성 인식(OpenAI) → 비서 세션(규칙/LLM/늘밥 모의 앱) → 문장마다 목소리(OpenAI TTS) → mp3
```

```bash
export NEULBAB_APP_DIR=/path/to/agent-company/product/app
python -m mobile_world.assistant.webapp --env-file /path/to/.env --self-signed
# → 늘밥 음성 비서: https://<서버 IP>:8443/   접속 코드: 123456
```

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--env-file` | `$OPENAI_API_KEY` | OpenAI 키가 든 파일. 키는 서버에만 있고 휴대폰으로 가지 않음 |
| `--model-name` / `--reasoning-effort` | `gpt-5` / `minimal` | 비서 LLM |
| `--stt-model` / `--tts-model` / `--tts-voice` | `gpt-4o-mini-transcribe` / `gpt-4o-mini-tts` / `coral` | 음성 인식·목소리. 목소리는 어르신께 말하듯 따뜻하고 조금 천천히 |
| `--self-signed` | 꺼짐 | 자체 서명 인증서로 HTTPS. 휴대폰 마이크는 HTTPS에서만 켜짐 |
| `--access-code` | 무작위 6자리 | 접속 번호. 모르는 사람이 API 비용을 쓰지 못하게 함 |
| `--state-file` | 없음 | 가족의 주문·설정을 파일에 남김 |

**화면과 동작**
- 큰 마이크 버튼을 누르고 말하면, 말을 멈추고 약 1.2초 뒤에 자동으로 보냅니다.
- 비서가 말하는 동안 마이크를 누르면 말을 멈추고 바로 듣습니다.
- 확인 질문("주문할까요?")에는 자동으로 귀를 열고, 큰 **[네] [아니요]** 버튼도 같이 보여 줍니다.
- 지금 주문 상태(메뉴, 단계, 도착 시각, 누가 주문했는지)가 5초마다 갱신됩니다. 어르신 폰과 보호자 폰이 같은 서버에 붙으면 같은 가족 상태를 봅니다.
- 마이크를 쓸 수 없으면 "글로 쓰기"로도 대화할 수 있습니다.
- 글자 22px 이상, 버튼 60px 이상, 간격 24px로 늘밥 고령자 UI 원칙을 따릅니다.

**휴대폰에서 열기:** 휴대폰이 서버 IP에 닿아야 합니다(같은 망이나 VPN). 자체 서명 인증서는 처음 열 때 "안전하지 않음" 경고가 나오고, "계속"을 눌러야 합니다. 경고 없이 쓰려면 정식 인증서(`--ssl-certfile`, `--ssl-keyfile`)를 쓰거나, HTTPS 프록시 뒤에 두세요.

**측정 (실제 OpenAI 음성으로 처음부터 끝까지):** 음성 인식 1.3~2.3초, 첫 문장 목소리 합성 약 1.4초입니다. 규칙으로 처리되는 요청도 첫 소리까지 3~4초 걸리고, LLM을 거치면 1초 뒤에 "잠시만요"가 먼저 나옵니다. 지금은 LLM보다 음성 처리가 더 깁니다.

## 온디바이스(앱 내장)로 옮기는 계획

인터페이스는 앱 안에서 그대로 구현할 수 있게 나눠 두었습니다.

| Python (지금) | Android 앱 (나중) |
|---|---|
| `SpeechToText` | `SpeechRecognizer`(온디바이스) 또는 Whisper(ONNX/TFLite) |
| `TextToSpeech` | `android.speech.tts.TextToSpeech` |
| `DeviceBackend.screenshot` | `PixelCopy` 또는 루트 View를 `Canvas`에 그리기 (내 앱이라 별도 권한 불필요) |
| `DeviceBackend.execute` | 좌표로 `MotionEvent`를 루트 View에 dispatch하거나, Compose semantics / View 트리로 클릭과 입력 |
| `current_package` / `AppScopeGuard` | 앱 프로세스 안에서만 동작하므로 앱을 벗어나는 일이 구조적으로 불가능. 외부 Intent만 막으면 됨 |
| `app_tools` deeplink/broadcast | 같은 이름과 스키마를 가진 Kotlin 함수를 직접 호출 |
| `InAppAssistantAgent` 프롬프트 | 같은 프롬프트를 서버(LLM 프록시)나 온디바이스 모델에서 사용 |

툴 이름과 JSON 스키마를 그대로 유지하면, 지금 MobileWorld에서 검증한 프롬프트와 동작을 앱 안에서도 그대로 쓸 수 있습니다.

## 테스트

```bash
uv run pytest tests/assistant -q
```
가짜 기기, 대본대로 움직이는 에이전트, 가짜 STT/TTS를 사용하므로 폰, LLM, 오디오 장치 없이 실행됩니다.
