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
