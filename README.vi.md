# gflow-cli (bản ABM)

[English](README.md) · **Tiếng Việt**

Tạo ảnh và video bằng [Google Flow](https://labs.google/fx/tools/flow) ngay trong terminal. gflow-cli điều khiển Imagen, Nano Banana (ảnh) và Veo, Omni Flash (video) qua chính tài khoản Flow của bạn. Cả một storyboard chạy bằng một lệnh, thay cho một buổi chiều ngồi bấm chuột.

Repo này là **bản ABM**, nhánh tách (fork) từ [ffroliva/gflow-cli](https://github.com/ffroliva/gflow-cli), do **ABM.DũngTQ** duy trì để phục vụ dây chuyền làm phim hoạt hình. Bản này giữ mọi tính năng của bản gốc (hiện là 0.91.0) và thêm: tải video 1080p, chạy Chrome ẩn trên macOS, chạy lô an toàn hơn và lệnh chạy lô video. Danh sách đầy đủ ở mục [Bản ABM có thêm gì](#bản-abm-có-thêm-gì).

[![Release](https://img.shields.io/github/v/release/abm-dungtq/gflow-cli?include_prereleases&label=release)](https://github.com/abm-dungtq/gflow-cli/releases)
[![Upstream](https://img.shields.io/badge/upstream-ffroliva%2Fgflow--cli%200.91.0-blue.svg)](https://github.com/ffroliva/gflow-cli)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Platforms](https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Windows%20%C2%B7%20Linux-lightgrey.svg)

[Công dụng](#công-dụng) · [Bản ABM có thêm gì](#bản-abm-có-thêm-gì) · [Cài đặt](#cài-đặt) · [Cách dùng](#cách-dùng) · [Chạy lô](#chạy-lô) · [Ẩn trình duyệt](#ẩn-trình-duyệt) · [Demo](#demo) · [Khi có lỗi](#khi-có-lỗi) · [Tài liệu](#tài-liệu) · [Tác giả](#tác-giả-và-ghi-công)

> ⚠️ **Đọc trước khi cài.** gflow-cli không liên quan đến Google. Công cụ này dò ngược cách Flow hoạt động và điều khiển một cửa sổ Chrome thật đã đăng nhập tài khoản Google *của bạn*, nên mọi thao tác tự động diễn ra trên tài khoản đó và chịu điều khoản của Google. Flow có thể đổi giao diện bất cứ lúc nào. Tạo ảnh không tốn credit; tạo video Veo trừ credit Flow của bạn. Hãy đọc [DISCLAIMER](DISCLAIMER.md) và [docs/ACCOUNT_SAFETY.md](docs/ACCOUNT_SAFETY.md).

## Công dụng

| Bạn muốn… | Lệnh |
|---|---|
| Tạo ảnh từ câu mô tả | `gflow image t2i "a red paper lantern on a wooden table"` |
| Sửa hoặc ghép ảnh | `gflow image i2i "same cat, now wearing a scarf" --ref cat.png` |
| Tạo video từ câu mô tả | `gflow video t2v "slow push-in on a forest clearing" --aspect 16:9` |
| Làm ảnh tĩnh chuyển động | `gflow video i2v "the lantern sways" --initial-frame lantern.jpg` |
| Tạo video từ ảnh tham chiếu | `gflow video r2v "..." --ref hero.png --ref room.png` |
| Lấy clip 1080p | thêm `--upscale` vào `t2v`, `i2v`, `r2v` hoặc `video batch` |
| Chạy cả storyboard ảnh | `gflow run --config storyboard.json` |
| Chạy nhiều clip ảnh-thành-video trong một phiên | `gflow video batch --config videos.json` |
| Giữ một nhân vật nhất quán qua nhiều cảnh | `gflow character create ...`, rồi tham chiếu nhân vật đó |
| Nối dài clip quá 8 giây | `gflow video extend <media_id> "the camera keeps rising"` |
| Xem số credit Veo | `gflow credits user` |
| Cho AI agent dùng Flow | `gflow mcp run` (stdio) hoặc `gflow serve` (HTTP) |
| Kiểm tra máy đã cài đúng chưa | `gflow doctor` |

Lệnh nào cũng nhận `--profile` để chọn tài khoản Google và `--project` để làm việc trong một project Flow có sẵn. Phần lớn lệnh có `--json` để dùng trong script. `gflow docs` in tài liệu đi kèm, không cần mạng.

Prompt nên viết bằng tiếng Anh: các mô hình của Flow hiểu tiếng Anh ổn định nhất.

## Bản ABM có thêm gì

So với bản gốc 0.91.0:

- **`--upscale`: giữ bản 1080p.** Tải video xong, gflow lấy bản xuất 1080p của Flow (không tốn thêm credit) và ghi đè lên file 720p ở đúng đường dẫn đó. Nếu xuất 1080p lỗi, file 720p vẫn được giữ và lệnh in ra câu lệnh để thử lại. Có ở `video t2v`, `i2v`, `r2v`, `video batch` và tool MCP `gflow_generate_video(upscale=True)`.
- **`gflow video batch`.** Chạy 1–50 dòng ảnh-thành-video từ một file JSON trong cùng một phiên trình duyệt và một project Flow, có `--resume`, `--jitter` và `--json`. Clip nào đã tạo xong, đã bị trừ credit nhưng tải về lỗi thì được tải lại miễn phí chứ không mất.
- **Chrome chạy ẩn trên macOS.** `GFLOW_CLI_WINDOW_MODE=background` chạy Chrome như một ứng dụng nền: không hiện biểu tượng trên Dock, không giành bàn phím khi bạn đang làm việc.
- **Lô dừng ngay khi Google chặn.** `gflow run`, `image batch` và `image t2i` nhiều prompt sẽ dừng ở lần đầu gặp WAF 403, bị giới hạn tốc độ hoặc phiên đăng nhập hết hạn, kể cả khi có `--continue-on-error`, và thoát với mã lỗi của chính lỗi đó. Tài khoản đã bị để ý sẽ không phải nhận tiếp phần còn lại của lô.
- **Đặt tên cho ảnh tham chiếu trong `gflow run`.** Khai báo `{"name": "cuong", "path": "cuong.png"}` rồi viết `@cuong` trong prompt. Mỗi dòng nhận được nhiều ảnh tham chiếu (`refs`) và có đường dẫn `output` riêng.
- **Tự gia hạn phiên đăng nhập.** Nếu phiên Google hết hạn khi chưa gửi gì, gflow gia hạn một lần rồi chạy lại. Đã gửi lệnh tạo rồi thì không bao giờ gửi lại. Tắt bằng `GFLOW_CLI_AUTO_RENEW=false`.
- **Sửa lỗi.** `-o` trỏ sang ổ đĩa khác không còn lỗi sau khi tạo xong. Lô ảnh dài tự tải lại ảnh tham chiếu đã trôi khỏi project. Tab Chrome chỉ được đưa lên trước khi nó thực sự bị ẩn.

Chi tiết ở [CHANGELOG.md](CHANGELOG.md) mục Unreleased. Số đo về chế độ không cửa sổ (headless) nằm ở [docs/superpowers/spikes/2026-10-28-headless-generation.md](docs/superpowers/spikes/2026-10-28-headless-generation.md).

## Cài đặt

### Cần có

- Một tài khoản Google dùng được Google Flow. Muốn tạo video thì tài khoản cần có credit Flow.
- [Google Chrome](https://www.google.com/chrome/) cài theo cách thông thường.
- [uv](https://docs.astral.sh/uv/getting-started/installation/); uv tự cài Python 3.11 trở lên.
- macOS, Windows hoặc Linux có giao diện đồ hoạ. Lần đăng nhập đầu tiên cần Chrome mở được cửa sổ.

### 1. Cài

```bash
uv tool install "git+https://github.com/abm-dungtq/gflow-cli.git@main"
uvx --from "git+https://github.com/abm-dungtq/gflow-cli.git@main" playwright install chromium
gflow --version
```

Muốn nâng cấp, chạy lại đúng lệnh `uv tool install` ở trên và thêm `--force`. Gói trên PyPI (`uv tool install gflow-cli`) là bản gốc, không có các tính năng của bản ABM.

### 2. Đăng nhập một lần

```bash
gflow auth login --browser chrome --profile main
```

Một cửa sổ Chrome sẽ mở ra. Đăng nhập Google và chờ đến khi thấy màn hình làm việc của Flow; gflow lưu phiên đăng nhập vào thư mục profile riêng của nó. Phải có `--browser chrome`, vì về sau mỗi lần tạo ảnh hay video, gflow mở lại profile này bằng Chrome thật. Mỗi tài khoản Google dùng một tên `--profile` khác nhau.

### 3. Kiểm tra

```bash
gflow auth status
gflow credits user --profile main
gflow doctor
```

Ba lệnh này không tốn credit. `gflow doctor` thoát với mã 33 khi thấy chỗ cần sửa và nói rõ phải sửa gì.

### 4. Cấu hình thêm (tuỳ chọn)

Chép [`.env.template`](.env.template) thành `.env` trong thư mục bạn chạy gflow, hoặc vào thư mục gốc của gflow để áp dụng cho cả máy. Các thiết lập hay dùng:

| Thiết lập | Tác dụng |
|---|---|
| `GFLOW_CLI_WINDOW_MODE=background` | macOS: chạy Chrome ẩn, không giành bàn phím |
| `GFLOW_CLI_OUTPUT_DIR=./out` | Nơi lưu file khi không truyền `-o` |
| `GFLOW_CLI_PROJECT=<id>` | Project Flow mặc định, để mỗi lần chạy không sinh project mới |
| `GFLOW_CLI_JITTER_RANGE=10-30` | Khoảng nghỉ giữa các lần gửi trong lô; nới rộng ra nếu gặp WAF 403 |
| `GFLOW_CLI_LEASE_WAIT_SECONDS=600` | Chờ khi profile đang bị một lệnh gflow khác dùng, thay vì báo lỗi |

Mọi thiết lập được giải thích trong [docs/CONFIGURATION.md](docs/CONFIGURATION.md). gflow chỉ đọc file `.env`; file `.env.local` bị bỏ qua.

## Cách dùng

### Ảnh (miễn phí)

```bash
gflow image t2i "a hot air balloon over Tokyo at sunrise" --aspect 16:9 -o out/balloon.png
gflow image t2i "a cat mom, flat 2D" --model nano2 -n 4 --out out/cats/
gflow image i2i "the same cockroach, now sitting on a keyboard" --ref refs/cuong.png -o out/k1.png
```

Mô hình: `nano2` (Nano Banana 2.1, mặc định), `nano2-lite`, `nano-pro`, `imagen4`. Tỉ lệ khung: `16:9`, `4:3`, `1:1`, `3:4`, `9:16`. `-n` tạo 1–4 biến thể.

### Video (tốn credit)

```bash
gflow video t2v "slow cinematic push-in on a sunlit forest clearing" --aspect 16:9 -o out/forest.mp4
gflow video i2v "the paper lantern sways gently, slow push in" \
  --initial-frame out/lantern.jpg --model veo-lite --aspect 16:9 --upscale -o out/lantern.mp4
gflow video upscale <media_id> --scale 1080p      # lấy 1080p cho clip đã làm trước đó
```

Mô hình: `omni-flash`, `veo-lite`, `veo-fast`, `veo-quality`. `--duration` nhận 4, 6 hoặc 8 giây; 10 giây chỉ có ở Omni Flash. `--end-frame` đặt khung hình cuối cho `i2v`. Nếu không truyền `--aspect`, video mặc định là dọc 9:16.

### Project, nhân vật và kho dữ liệu

```bash
gflow project list --profile main          # các project gflow đã ghi lại
gflow character create --project <id> --name "Aria" --face-prompt "..." --body-prompt "..."
gflow data --help                          # tra cứu mọi thứ bạn đã tạo
```

### Dành cho AI agent (MCP)

```bash
gflow mcp setup          # đăng ký server stdio với agent của bạn
gflow mcp run --no-spend # server không có các tool tạo ảnh, video
```

CLI và server MCP có cùng một bộ tính năng. [docs/MCP.md](docs/MCP.md) liệt kê đủ các tool.

## Chạy lô

### Storyboard ảnh: `gflow run`

`storyboard.json`:

```json
{
  "prompts": [
    {"text": "@cuong stands on a desk at night, flat 2D", "aspect_ratio": "16:9",
     "refs": [{"name": "cuong", "path": "refs/cuong.png"}], "output": "gen/k1.jpg"},
    {"text": "@cuong jumps onto the keyboard", "aspect_ratio": "16:9",
     "refs": [{"name": "cuong", "path": "refs/cuong.png"}], "output": "gen/k2.jpg"}
  ]
}
```

```bash
gflow run --config storyboard.json --profile main --project <id>
gflow run --config storyboard.json --profile main --project <id> --resume
```

Mỗi file ảnh tham chiếu chỉ tải lên một lần cho cả lượt chạy. `--resume` bỏ qua những dòng đã có file đầu ra, nên chạy lại sau khi lỗi chỉ tạo phần còn thiếu.

### Nhiều clip ảnh-thành-video trong một phiên: `gflow video batch`

`videos.json`:

```json
{
  "project": "<existing-flow-project-id>",
  "videos": [
    {"start": "gen/k1.jpg", "prompt": "the cockroach looks around, slow push in",
     "model": "omni-flash", "duration": 4, "aspect": "16:9", "output": "clips/v1.mp4"},
    {"start": "gen/k2.jpg", "prompt_file": "prompts/v2.txt",
     "model": "omni-flash", "duration": 6, "aspect": "16:9", "output": "clips/v2.mp4"}
  ]
}
```

```bash
gflow video batch --config videos.json --profile main --upscale --json
```

Dòng nào đã gửi thì không bao giờ gửi lại, vì clip Flow đã nhận có thể đã bị trừ credit. Nếu một dòng lỗi, chạy lại với `--resume`.

## Ẩn trình duyệt

Google chỉ phục vụ Flow cho trình duyệt thật, nên gflow phải điều khiển một cửa sổ Chrome thật. Bạn nhìn thấy nó nhiều hay ít tuỳ hệ điều hành:

| Hệ điều hành | Thiết lập | Kết quả |
|---|---|---|
| macOS | `GFLOW_CLI_WINDOW_MODE=background` | Ẩn hoàn toàn, không có biểu tượng trên Dock, không bao giờ giành bàn phím. Nên dùng. |
| macOS, Windows, Linux | `offscreen` (mặc định) | Cửa sổ mở ở ngoài màn hình, nhưng Chrome giành bàn phím lúc khởi động. |
| mọi hệ | `GFLOW_CLI_HEADLESS=true` | Không có cửa sổ nào. Đang thử nghiệm, xem bên dưới. |
| mọi hệ | `normal` | Cửa sổ hiện bình thường, tiện khi muốn xem gflow làm gì. |

Trên một tài khoản thử (macOS, 28/10/2026), Chrome thật chạy headless tạo ảnh thành công 6/6 lần, không gặp 403 hay lỗi reCAPTCHA. Tuy vậy user agent của nó ghi rõ `HeadlessChrome`. Ngoài ra chưa ai đo với video, trên Windows hay về lâu dài với tài khoản. Vì thế mặc định vẫn là Chrome có cửa sổ. Muốn dùng headless, hãy thử trên tài khoản phụ trước.

## Demo

![gflow image t2i chạy một prompt 9:16, in log và ghi ra một file PNG](docs/assets/example-run.gif)

Một lượt `gflow run` thật gồm hai dòng, trên tài khoản thử (29/10/2026, khoảng một phút, không tốn credit):

```text
$ gflow run --config run.json --profile thu-headless --project <project-id> --output-dir tmp/f7/out
  project: <project-id> (existing)
                                   gflow run
┏━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ # ┃ prompt                      ┃ ratio ┃ status ┃ detail                    ┃
┡━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 0 │ a small red paper lantern   │ 16:9  │ OK     │ tmp/f7/out/prompt_0_0.jpg │
│   │ on a wooden table, soft     │       │        │                           │
│   │ light                       │       │        │                           │
│ 1 │ a green ceramic teapot on a │ 16:9  │ OK     │ tmp/f7/out/prompt_1_0.jpg │
│   │ windowsill, morning light   │       │        │                           │
└───┴─────────────────────────────┴───────┴────────┴───────────────────────────┘

2/2 succeeded · 0 failure(s) · 0 skipped
```

Sau đó ảnh đèn lồng đầu tiên được làm thành video bằng `gflow video i2v --model veo-lite --upscale` trên cùng tài khoản. Kết quả là một clip 4 giây, độ phân giải 1920×1080; kết quả JSON có `"upscaled": "1080p"`.

![Ví dụ: tạo ảnh từ chữ và biến đổi khung hình trước/sau](docs/assets/examples.webp)

## Khi có lỗi

Khi lỗi, gflow dừng hẳn và trả về một mã thoát cố định chứ không tự gửi lại, để lượt chạy hỏng không âm thầm tiêu credit.

| Mã thoát | Ý nghĩa | Nên làm gì |
|---|---|---|
| 3 / 8 | Phiên đăng nhập hết hạn hoặc chưa có | `gflow auth renew`, hoặc chạy lại `gflow auth login` |
| 4 | Bị giới hạn tốc độ | Chờ một lúc, rồi nới `--jitter` |
| 5 | Bị chính sách nội dung từ chối | Viết lại prompt đó; lô vẫn chạy tiếp các dòng khác |
| 10 | WAF 403: Google chặn yêu cầu | Dừng lại và chờ. Đừng chạy lại liên tục trên tài khoản này |
| 11 | Sai cấu hình, hoặc profile đang bận | Đọc thông báo; nó ghi rõ trường nào sai hoặc ai đang giữ khoá |
| 23 | Flow đã đổi giao diện | Chạy `gflow update`, rồi báo lỗi |
| 37 | Không đủ credit Veo | Nạp thêm, hoặc dùng mô hình rẻ hơn |

Mọi lỗi được in theo chuẩn RFC 9457 (problem details) kèm gợi ý cách xử lý. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) liệt kê các lỗi hiện có và cách né.

## Tài liệu

Tài liệu chi tiết hiện chỉ có bằng tiếng Anh.

| Chủ đề | Đọc |
|---|---|
| Bắt đầu | [Hướng dẫn sử dụng](docs/USER_GUIDE.md) · [Mọi lệnh](docs/USAGE.md) · [Cấu hình](docs/CONFIGURATION.md) |
| Tài khoản | [Đăng nhập](docs/AUTHENTICATION.md) · [An toàn tài khoản](docs/ACCOUNT_SAFETY.md) · [Lỗi đã biết](KNOWN_ISSUES.md) |
| Sản xuất | [Nhân vật](docs/CHARACTER.md) · [Kịch bản nhiều cảnh](docs/MOVIE.md) · [Công cụ viết prompt](docs/TOOLS.md) · [Cách tham chiếu ảnh](docs/REFERENCE_STRATEGIES.md) |
| Agent | [Server MCP](docs/MCP.md) · [AGENTS.md](AGENTS.md) · [llms.txt](llms.txt) |
| Bên trong | [Kiến trúc](docs/ARCHITECTURE.md) · [Phát triển](docs/DEVELOPMENT.md) · [Changelog](CHANGELOG.md) |

[docs/INDEX.md](docs/INDEX.md) là mục lục của toàn bộ tài liệu.

## Ủng hộ dự án gốc

Bản gốc gflow-cli do một người làm, và mỗi bản phát hành ở đó đều được thử trên Flow thật bằng credit thật. Nếu công cụ này giúp bạn tiết kiệm thời gian, hãy ủng hộ tác giả gốc qua [GitHub Sponsors](https://github.com/sponsors/ffroliva). Các mức ủng hộ có trong [docs/SPONSORS.md](docs/SPONSORS.md).

## Tác giả và ghi công

- **Bản ABM:** ABM.DũngTQ · 0976202028 · [github.com/abm-dungtq/gflow-cli](https://github.com/abm-dungtq/gflow-cli)
- **Dự án gốc:** [gflow-cli](https://github.com/ffroliva/gflow-cli) của Flavio Oliva (`ffroliva`). Phần lõi của công cụ này là công sức của Flavio Oliva; bản sửa nào dùng được cho bản gốc thì nên gửi về đó.
- **Giấy phép:** [MIT](LICENSE), © 2026 Flavio Oliva, giữ nguyên trong bản fork này. Giấy phép chỉ áp dụng cho mã nguồn. Flow và mọi thứ bạn tạo bằng Flow chịu điều khoản riêng của Google. Xem [DISCLAIMER](DISCLAIMER.md).
