def get_live_html(api_key: str, model: str) -> str:
    return f"""
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: transparent; }}
        .controls {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }}
        .btn {{
            padding: 10px 24px; border: none; border-radius: 8px; font-size: 15px;
            font-weight: 600; cursor: pointer; transition: all 0.2s;
        }}
        .btn-start {{ background: #10b981; color: white; }}
        .btn-start:hover {{ background: #059669; }}
        .btn-stop {{ background: #ef4444; color: white; }}
        .btn-stop:hover {{ background: #dc2626; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .status {{
            display: inline-flex; align-items: center; gap: 8px;
            padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 500;
        }}
        .status-idle {{ background: #f3f4f6; color: #6b7280; }}
        .status-listening {{ background: #dcfce7; color: #16a34a; }}
        .pulse {{
            width: 10px; height: 10px; border-radius: 50%; background: #16a34a;
            animation: pulse 1.5s ease-in-out infinite;
        }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: 0.5; transform: scale(1.3); }} }}
        .visualizer {{
            width: 100%; height: 60px; border-radius: 8px; background: #f9fafb;
            margin-bottom: 16px; display: none;
        }}
        .visualizer.active {{ display: block; }}
        .transcript-box {{
            background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px;
            padding: 16px; min-height: 200px; max-height: 500px; overflow-y: auto;
            font-size: 15px; line-height: 1.8;
        }}
        .interim {{ color: #9ca3af; font-style: italic; }}
        .final-text {{ color: #1f2937; }}
        .download-btn {{
            margin-top: 12px; padding: 8px 20px; background: #3b82f6; color: white;
            border: none; border-radius: 6px; cursor: pointer; font-size: 13px;
        }}
        .download-btn:hover {{ background: #2563eb; }}
    </style>

    <div class="controls">
        <button class="btn btn-start" id="startBtn" onclick="startListening()">🎤 Start Listening</button>
        <button class="btn btn-stop" id="stopBtn" onclick="stopListening()" disabled>⏹ Stop</button>
        <span class="status status-idle" id="status">Ready</span>
    </div>

    <canvas class="visualizer" id="visualizer"></canvas>

    <div class="transcript-box" id="transcript">
        <p style="color: #9ca3af;">Transcript will appear here as you speak...</p>
    </div>

    <button class="download-btn" id="downloadBtn" onclick="downloadTranscript()" style="display:none;">📥 Download Transcript</button>

    <script>
        const API_KEY = "{api_key}";
        const MODEL = "{model}";
        let socket, audioContext, analyser, animFrame;
        let finalTranscript = "";
        let interimText = "";

        function startListening() {{
            navigator.mediaDevices.getUserMedia({{ audio: true }}).then(stream => {{
                audioContext = new AudioContext();
                const source = audioContext.createMediaStreamSource(stream);
                analyser = audioContext.createAnalyser();
                analyser.fftSize = 256;
                source.connect(analyser);
                const canvas = document.getElementById('visualizer');
                canvas.classList.add('active');
                drawVisualizer(canvas, analyser);

                const params = new URLSearchParams({{
                    model: MODEL, language: "en-IN", smart_format: "true",
                    interim_results: "true", endpointing: "300",
                    encoding: "linear16", sample_rate: audioContext.sampleRate.toString(),
                    channels: "1", punctuate: "true"
                }});
                socket = new WebSocket("wss://api.deepgram.com/v1/listen?" + params.toString(), ["token", API_KEY]);

                socket.onopen = () => {{
                    document.getElementById('startBtn').disabled = true;
                    document.getElementById('stopBtn').disabled = false;
                    document.getElementById('status').className = 'status status-listening';
                    document.getElementById('status').innerHTML = '<span class="pulse"></span> Listening...';

                    const processor = audioContext.createScriptProcessor(4096, 1, 1);
                    source.connect(processor);
                    processor.connect(audioContext.destination);
                    processor.onaudioprocess = (e) => {{
                        if (socket && socket.readyState === WebSocket.OPEN) {{
                            const input = e.inputBuffer.getChannelData(0);
                            const pcm16 = new Int16Array(input.length);
                            for (let i = 0; i < input.length; i++) {{
                                pcm16[i] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                            }}
                            socket.send(pcm16.buffer);
                        }}
                    }};
                    window._processor = processor;
                    window._source = source;
                }};

                socket.onmessage = (event) => {{
                    const data = JSON.parse(event.data);
                    if (data.type === "Results") {{
                        const alt = data.channel?.alternatives?.[0];
                        if (alt && alt.transcript) {{
                            if (data.is_final) {{
                                finalTranscript += alt.transcript + " ";
                                interimText = "";
                            }} else {{
                                interimText = alt.transcript;
                            }}
                            renderTranscript();
                        }}
                    }}
                }};

                socket.onerror = () => {{ stopListening(); }};
                socket.onclose = () => {{ }};
                window._stream = stream;
            }}).catch(err => {{
                alert("Microphone access denied: " + err.message);
            }});
        }}

        function stopListening() {{
            if (socket && socket.readyState === WebSocket.OPEN) {{
                socket.send(JSON.stringify({{ type: "CloseStream" }}));
                socket.close();
            }}
            if (window._processor) {{ window._processor.disconnect(); }}
            if (window._source) {{ window._source.disconnect(); }}
            if (window._stream) {{ window._stream.getTracks().forEach(t => t.stop()); }}
            if (audioContext) {{ audioContext.close(); }}
            if (animFrame) {{ cancelAnimationFrame(animFrame); }}

            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            document.getElementById('status').className = 'status status-idle';
            document.getElementById('status').textContent = 'Stopped';
            document.getElementById('visualizer').classList.remove('active');
            if (finalTranscript.trim().length > 0) {{
                document.getElementById('downloadBtn').style.display = 'inline-block';
            }}
        }}

        function renderTranscript() {{
            const box = document.getElementById('transcript');
            let html = '';
            if (finalTranscript.trim()) {{
                html += '<span class="final-text">' + finalTranscript + '</span>';
            }}
            if (interimText) {{
                html += '<span class="interim">' + interimText + '</span>';
            }}
            box.innerHTML = html || '<p style="color: #9ca3af;">Transcript will appear here as you speak...</p>';
            box.scrollTop = box.scrollHeight;
        }}

        function drawVisualizer(canvas, analyser) {{
            const ctx = canvas.getContext('2d');
            const bufferLength = analyser.frequencyBinCount;
            const dataArray = new Uint8Array(bufferLength);
            const WIDTH = canvas.width = canvas.offsetWidth;
            const HEIGHT = canvas.height = 60;

            function draw() {{
                animFrame = requestAnimationFrame(draw);
                analyser.getByteTimeDomainData(dataArray);
                ctx.fillStyle = '#f9fafb';
                ctx.fillRect(0, 0, WIDTH, HEIGHT);
                ctx.lineWidth = 2;
                ctx.strokeStyle = '#10b981';
                ctx.beginPath();
                const sliceWidth = WIDTH / bufferLength;
                let x = 0;
                for (let i = 0; i < bufferLength; i++) {{
                    const v = dataArray[i] / 128.0;
                    const y = (v * HEIGHT) / 2;
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                    x += sliceWidth;
                }}
                ctx.lineTo(WIDTH, HEIGHT / 2);
                ctx.stroke();
            }}
            draw();
        }}

        function downloadTranscript() {{
            const blob = new Blob([finalTranscript.trim()], {{ type: 'text/plain' }});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'live_transcript.txt';
            a.click();
        }}
    </script>
    """
