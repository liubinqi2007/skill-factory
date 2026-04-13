// ═══ Skill Factory — WebSocket Chat ═══

var skills = [];
var activeSkillId = null;
var ws = null;

// ── 空闲检测 ──
var WS_IDLE_THRESHOLD = 5 * 60 * 1000;   // 5分钟WS无数据流，进入空闲警戒
var VISIBILITY_TIMEOUT = 60 * 1000;       // 空闲警戒后，页面切后台60秒则断开
var USER_IDLE_TIMEOUT = 10 * 60 * 1000;   // 空闲警戒后，用户无操作10分钟则断开
var _lastWsActivity = Date.now();         // 最后一次WS数据流时间
var _idleCheckTimer = null;
var _visibilityTimer = null;
var _idleDisconnected = false;

// WS有数据流时调用（收发消息均触发）
function _onWsActivity() {
    _lastWsActivity = Date.now();
    // 有数据流，取消所有空闲检测计时器
    if (_idleCheckTimer) { clearTimeout(_idleCheckTimer); _idleCheckTimer = null; }
    if (_visibilityTimer) { clearTimeout(_visibilityTimer); _visibilityTimer = null; }
}

// 启动空闲检查（WS连接建立时调用）
function _startIdleCheck() {
    _lastWsActivity = Date.now();
    if (_idleCheckTimer) clearTimeout(_idleCheckTimer);
    _idleCheckTimer = setTimeout(_checkWsIdle, 30000);  // 每30秒检查一次WS数据流
}

function _checkWsIdle() {
    if (_idleDisconnected) return;
    if (!activeSkillId || !ws || ws.readyState !== WebSocket.OPEN) return;
    var elapsed = Date.now() - _lastWsActivity;
    if (elapsed >= WS_IDLE_THRESHOLD) {
        // WS已5分钟无数据流，进入警戒：检查页面可见性和用户操作
        _onWsIdleAlert();
    } else {
        _idleCheckTimer = setTimeout(_checkWsIdle, 30000);
    }
}

function _onWsIdleAlert() {
    // 立即检查：如果页面当前在后台，60秒后断开
    if (document.hidden) {
        _visibilityTimer = setTimeout(function() {
            if (document.hidden) _idleDisconnect('visibility');
        }, VISIBILITY_TIMEOUT);
    }

    // 同时启动用户操作超时检测：再无操作10分钟则断开
    _idleCheckTimer = setTimeout(function() {
        if (!_idleDisconnected && activeSkillId && ws && ws.readyState === WebSocket.OPEN) {
            _idleDisconnect('idle');
        }
    }, USER_IDLE_TIMEOUT);
}

function _idleDisconnect(reason) {
    _idleDisconnected = true;
    // 关闭 WebSocket
    if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
    }
    // 显示弹窗
    var modal = document.getElementById('idleModal');
    var desc = document.getElementById('idleModalDesc');
    if (reason === 'visibility') {
        desc.textContent = '检测到页面切换到后台，为节省资源已自动断开连接。';
    } else {
        desc.textContent = '页面长时间未操作，为节省资源已自动断开连接。';
    }
    modal.style.display = '';
}

function _idleReconnect() {
    var modal = document.getElementById('idleModal');
    modal.style.display = 'none';
    _idleDisconnected = false;
    _lastWsActivity = Date.now();
    if (activeSkillId) {
        connectWS(activeSkillId);
    }
}

window.quickCreate = quickCreate;
window.selectSkill = selectSkill;
window.loadSkills = loadSkills;
window.loadStatus = loadStatus;
window.renderSkillList = renderSkillList;

// ── Init ───────────────────────────────────────────
window.addEventListener('load', initApp);

function initApp() {
    loadSkills();
    loadStatus();
    setInterval(loadStatus, 5000);
    setInterval(loadSkills, 3000);

    var quickBtn = document.getElementById('quickBtn');
    var quickInput = document.getElementById('quickInput');
    var sendBtn = document.getElementById('sendBtn');
    var chatInput = document.getElementById('chatInput');
    var btnNew = document.getElementById('btnNew');

    if (quickBtn) quickBtn.addEventListener('click', quickCreate);

    // 快速创建输入框 - IME 安全
    if (quickInput) {
        var quickComposing = false;
        quickInput.addEventListener('compositionstart', function() { quickComposing = true; });
        quickInput.addEventListener('compositionend', function() { quickComposing = false; });
        quickInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !quickComposing) { e.preventDefault(); quickCreate(); }
        });
    }

    if (sendBtn) sendBtn.addEventListener('click', sendMessage);

    // 聊天输入框 - IME 安全
    if (chatInput) {
        var chatComposing = false;
        chatInput.addEventListener('compositionstart', function() { chatComposing = true; });
        chatInput.addEventListener('compositionend', function() { chatComposing = false; });
        chatInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey && !chatComposing) { e.preventDefault(); sendMessage(); }
        });
        chatInput.addEventListener('input', autoResize);
    }

    // +号按钮 - 切回创建页面
    if (btnNew) btnNew.addEventListener('click', function() {
        activeSkillId = null;
        document.getElementById('emptyState').style.display = '';
        document.getElementById('chatArea').style.display = 'none';
        renderSkillList();
        var input = document.getElementById('quickInput');
        if (input) input.focus();
    });

    // 页面可见性变化检测（仅WS空闲警戒后生效）
    document.addEventListener('visibilitychange', function() {
        if (document.hidden) {
            // 页面切到后台，仅在WS已空闲5分钟时才开始60秒倒计时
            if (!_idleDisconnected && activeSkillId && ws && ws.readyState === WebSocket.OPEN) {
                var wsElapsed = Date.now() - _lastWsActivity;
                if (wsElapsed >= WS_IDLE_THRESHOLD && !_visibilityTimer) {
                    _visibilityTimer = setTimeout(function() {
                        if (document.hidden) _idleDisconnect('visibility');
                    }, VISIBILITY_TIMEOUT);
                }
            }
        } else {
            // 页面回到前台，取消 visibility 断开计时
            if (_visibilityTimer) { clearTimeout(_visibilityTimer); _visibilityTimer = null; }
        }
    });

    // 重连按钮
    var reconnectBtn = document.getElementById('idleReconnectBtn');
    if (reconnectBtn) reconnectBtn.addEventListener('click', _idleReconnect);
}

function autoResize() {
    var ta = document.getElementById('chatInput');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
}

// ── WebSocket ──────────────────────────────────────
function connectWS(skillId) {
    if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
    }

    var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = protocol + '//' + location.host + '/ws/chat/' + skillId;
    ws = new WebSocket(url);

    ws.onopen = function() {
        console.log('WS connected:', skillId);
        document.getElementById('sendBtn').disabled = false;
        _onWsActivity();
        _startIdleCheck();
    };

    ws.onmessage = function(event) {
        try {
            var data = JSON.parse(event.data);
            _onWsActivity();  // 收到消息，重置WS空闲计时
            handleWSMessage(data);
        } catch(e) {
            console.error('WS parse error:', e);
        }
    };

    ws.onclose = function() {
        console.log('WS closed');
        document.getElementById('sendBtn').disabled = true;
        ws = null;
    };

    ws.onerror = function(err) {
        console.error('WS error:', err);
        document.getElementById('sendBtn').disabled = true;
    };
}

// ── 消息渲染状态 ──
var currentResponse = null;

function handleWSMessage(data) {
    if (data.type === 'status') {
        handleStatus(data);
    }
    else if (data.type === 'tool_status') {
        handleToolStatus(data);
    }
    else if (data.type === 'question') {
        handleQuestion(data);
    }
    else if (data.type === 'thinking') {
        handleThinking(data);
    }
    else if (data.type === 'text') {
        handleText(data);
    }
    else if (data.type === 'done') {
        handleDone(data);
    }
    else if (data.type === 'error') {
        handleError(data);
    }
}

function ensureResponse() {
    if (currentResponse) return currentResponse;

    var messagesEl = document.getElementById('messages');
    var container = document.createElement('div');
    container.className = 'ai-response streaming';
    messagesEl.appendChild(container);

    currentResponse = {
        container: container,
        indicator: null,
        thinkingText: '',
        textEl: null,
        textContent: '',
        toolBar: null
    };

    return currentResponse;
}

function handleStatus(data) {
    var resp = ensureResponse();
    var content = data.content || '';
    var isThinking = content.indexOf('思考') >= 0 || content.indexOf('thinking') >= 0;

    if (isThinking) {
        // 如果已有思考块且正在输出文本，先折叠旧的
        if (resp.indicator && resp.textContent) {
            finishThinkingBlock(resp);
        }
        // 每次新思考创建新的块
        createThinkingBlock(resp);
    }

    // 更新或创建加载指示器
    updateLoadingIndicator(resp, content);

    scrollToBottom();
}

function updateLoadingIndicator(resp, statusText) {
    // 如果已有文本输出，不需要加载指示器
    if (resp.textContent) {
        removeLoadingIndicator(resp);
        return;
    }

    // 查找或创建加载指示器
    var loader = resp.container.querySelector('.loading-indicator');
    if (!loader) {
        loader = document.createElement('div');
        loader.className = 'loading-indicator';
        loader.innerHTML =
            '<div class="loading-dots">' +
                '<span class="dot"></span><span class="dot"></span><span class="dot"></span>' +
            '</div>' +
            '<span class="loading-text"></span>';
        resp.container.appendChild(loader);
    }

    var textEl = loader.querySelector('.loading-text');
    if (textEl && statusText) {
        textEl.textContent = statusText;
    }
}

function removeLoadingIndicator(resp) {
    if (!resp) return;
    var loader = resp.container.querySelector('.loading-indicator');
    if (loader) loader.remove();
}

function handleToolStatus(data) {
    var resp = ensureResponse();
    var tool = data.tool || '';
    var status = data.status || '';
    var detail = data.detail || '';

    // 找到或创建工具状态容器
    if (!resp.toolBar) {
        var bar = document.createElement('div');
        bar.className = 'tool-status-bar';
        if (resp.textEl && resp.textEl.parentNode === resp.container) {
            resp.container.insertBefore(bar, resp.textEl);
        } else {
            resp.container.appendChild(bar);
        }
        resp.toolBar = bar;
    }

    var item = document.createElement('div');
    var statusClass = status === 'completed' ? 'tool-completed' : status === 'running' ? 'tool-running' : 'tool-pending';
    item.className = 'tool-item ' + statusClass;

    var icon = status === 'completed' ? '&#10003;' : status === 'running' ? '&#9654;' : '&#9679;';
    var label = tool;
    if (status === 'running') label += ' (执行中...)';
    else if (status === 'completed' && detail) label += ' - ' + detail;

    item.innerHTML = '<span class="tool-icon">' + icon + '</span><span class="tool-name">' + escapeHtml(label) + '</span>';

    if (status === 'completed') {
        var existing = resp.toolBar.querySelectorAll('.tool-item:not(.tool-completed)');
        for (var i = 0; i < existing.length; i++) {
            var nameEl = existing[i].querySelector('.tool-name');
            if (nameEl && nameEl.textContent.indexOf(tool) === 0) {
                existing[i].remove();
            }
        }
    }

    resp.toolBar.appendChild(item);
    scrollToBottom();
}

// ── Question 处理 ─────────────────────────────────
function handleQuestion(data) {
    var resp = ensureResponse();
    var requestId = data.request_id || '';
    var questions = data.questions || [];

    // 折叠当前思考块
    finishThinkingBlock(resp);
    removeCursor();

    // 创建问题卡片
    var card = document.createElement('div');
    card.className = 'question-card';
    card.setAttribute('data-request-id', requestId);

    var header = document.createElement('div');
    header.className = 'question-header';
    header.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
        '<span class="question-title">AI 有问题想问你</span>';
    card.appendChild(header);

    for (var qi = 0; qi < questions.length; qi++) {
        var q = questions[qi];
        var block = document.createElement('div');
        block.className = 'question-block';
        block.setAttribute('data-q-index', qi);

        var label = document.createElement('div');
        label.className = 'question-label';
        label.textContent = q.header ? q.header + '：' + q.question : q.question;
        block.appendChild(label);

        var opts = q.options || [];
        if (opts.length > 0) {
            var optList = document.createElement('div');
            optList.className = 'question-options';
            for (var oi = 0; oi < opts.length; oi++) {
                (function(opt, qIdx) {
                    var btn = document.createElement('button');
                    btn.className = 'question-option-btn';
                    btn.innerHTML = '<span class="option-label">' + escapeHtml(opt.label) + '</span>' +
                        (opt.description ? '<span class="option-desc">' + escapeHtml(opt.description) + '</span>' : '');
                    btn.addEventListener('click', function() {
                        _submitQuestionAnswer(requestId, qIdx, opt.label, card);
                    });
                    optList.appendChild(btn);
                })(opts[oi], qi);
            }
            block.appendChild(optList);
        } else {
            // 自由文本输入
            var input = document.createElement('div');
            input.className = 'question-input-wrapper';
            var textInput = document.createElement('input');
            textInput.type = 'text';
            textInput.className = 'question-text-input';
            textInput.placeholder = '输入你的回答...';
            textInput.setAttribute('data-q-index', qi);
            var submitBtn = document.createElement('button');
            submitBtn.className = 'question-submit-btn';
            submitBtn.textContent = '回答';
            submitBtn.addEventListener('click', function() {
                var val = textInput.value.trim();
                if (val) _submitQuestionAnswer(requestId, qi, val, card);
            });
            textInput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    var val = textInput.value.trim();
                    if (val) _submitQuestionAnswer(requestId, qi, val, card);
                }
            });
            input.appendChild(textInput);
            input.appendChild(submitBtn);
            block.appendChild(input);
        }

        card.appendChild(block);
    }

    // 插入到容器中（在 text 区域之前）
    if (resp.textEl && resp.textEl.parentNode === resp.container) {
        resp.container.insertBefore(card, resp.textEl);
    } else {
        resp.container.appendChild(card);
    }

    document.getElementById('sendBtn').disabled = false;
    scrollToBottom();
}

function _submitQuestionAnswer(requestId, qIndex, answer, card) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    // 构造 answers 数组：[[answer1], [answer2], ...]
    // 先收集已有的回答
    var existingAnswers = card._answers || {};
    existingAnswers[qIndex] = answer;
    card._answers = existingAnswers;

    // 标记已回答的问题块
    var blocks = card.querySelectorAll('.question-block');
    blocks[qIndex].classList.add('answered');

    // 更新已选择的选项样式
    var btns = blocks[qIndex].querySelectorAll('.question-option-btn');
    btns.forEach(function(b) {
        var labelEl = b.querySelector('.option-label');
        if (labelEl && labelEl.textContent === answer) {
            b.classList.add('selected');
        } else {
            b.classList.remove('selected');
            b.disabled = true;
        }
    });

    // 检查是否所有问题都已回答
    var allAnswered = true;
    for (var i = 0; i < blocks.length; i++) {
        if (existingAnswers[i] === undefined) {
            allAnswered = false;
            break;
        }
    }

    if (allAnswered) {
        // 构造最终 answers 数组
        var answers = [];
        for (var j = 0; j < blocks.length; j++) {
            answers.push([existingAnswers[j]]);
        }

        // 发送回答
        ws.send(JSON.stringify({
            type: 'question_reply',
            request_id: requestId,
            answers: answers
        }));

        // 显示已回答状态
        card.classList.add('question-answered');
        var statusEl = document.createElement('div');
        statusEl.className = 'question-answer-status';
        var answerTexts = [];
        for (var k = 0; k < answers.length; k++) {
            answerTexts.push(answers[k].join(', '));
        }
        statusEl.textContent = '已回答: ' + answerTexts.join('; ');
        card.appendChild(statusEl);

        // 禁用所有未选中的按钮
        card.querySelectorAll('.question-option-btn').forEach(function(b) {
            if (!b.classList.contains('selected')) b.disabled = true;
        });

        document.getElementById('sendBtn').disabled = true;
    }
}

function createThinkingBlock(resp) {
    var block = document.createElement('div');
    block.className = 'thinking-block open';
    block.innerHTML =
        '<div class="thinking-header" onclick="this.parentElement.classList.toggle(\'open\');this.parentElement.classList.toggle(\'closed\')">' +
            '<svg class="thinking-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
            '<span class="thinking-label">思考中</span>' +
            '<span class="thinking-spinner"></span>' +
            '<svg class="thinking-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</div>' +
        '<div class="thinking-body"></div>';
    // thinking 块始终插入到 text 区域前面，保证所有思考块在回复上方
    if (resp.textEl && resp.textEl.parentNode === resp.container) {
        resp.container.insertBefore(block, resp.textEl);
    } else {
        resp.container.appendChild(block);
    }
    resp.indicator = block;
    resp.thinkingText = '';
}

function finishThinkingBlock(resp) {
    if (!resp.indicator) return;

    var body = resp.indicator.querySelector('.thinking-body');
    var hasContent = body && body.textContent.trim().length > 0;

    if (hasContent) {
        // 有内容：折叠，改标签，停 spinner
        resp.indicator.classList.remove('open');
        resp.indicator.classList.add('closed');
        var label = resp.indicator.querySelector('.thinking-label');
        if (label) label.textContent = '思考过程';
        var spinner = resp.indicator.querySelector('.thinking-spinner');
        if (spinner) spinner.remove();
    } else {
        // 空思考块：直接删除
        resp.indicator.remove();
    }

    resp.indicator = null;
    resp.thinkingText = '';
}

function handleThinking(data) {
    var resp = ensureResponse();
    // 移除加载指示器
    removeLoadingIndicator(resp);
    // 如果没有活跃的思考块，创建一个
    if (!resp.indicator) {
        createThinkingBlock(resp);
    }

    resp.thinkingText += data.content || '';

    var body = resp.indicator.querySelector('.thinking-body');
    body.textContent = resp.thinkingText;
    body.scrollTop = body.scrollHeight;
    scrollToBottom();
}

function handleText(data) {
    var resp = ensureResponse();

    // 移除加载指示器
    removeLoadingIndicator(resp);
    // 折叠并结束当前思考块
    finishThinkingBlock(resp);

    // 创建文本区域
    if (!resp.textEl) {
        var textBlock = document.createElement('div');
        textBlock.className = 'response-text';
        resp.container.appendChild(textBlock);
        resp.textEl = textBlock;
    }

    resp.textContent += data.content;
    resp.textEl.innerHTML = renderMarkdown(resp.textContent);

    // 添加流式光标
    removeCursor();
    var cursor = document.createElement('span');
    cursor.className = 'streaming-cursor';
    cursor.textContent = '▊';
    resp.textEl.appendChild(cursor);

    scrollToBottom();
}

function handleDone(data) {
    var resp = currentResponse;
    if (!resp) return;

    // 移除加载指示器
    removeLoadingIndicator(resp);
    // 折叠并结束当前思考块
    finishThinkingBlock(resp);
    // 确保所有思考块的 spinner 都停止
    var allSpinners = resp.container.querySelectorAll('.thinking-spinner');
    for (var i = 0; i < allSpinners.length; i++) {
        allSpinners[i].remove();
    }
    // 确保所有未折叠的思考块都折叠
    var openBlocks = resp.container.querySelectorAll('.thinking-block.open');
    for (var j = 0; j < openBlocks.length; j++) {
        openBlocks[j].classList.remove('open');
        openBlocks[j].classList.add('closed');
        var lbl = openBlocks[j].querySelector('.thinking-label');
        if (lbl) lbl.textContent = '思考过程';
    }

    // 移除光标
    removeCursor();

    // 渲染最终 markdown
    if (resp.textEl && resp.textContent) {
        resp.textEl.innerHTML = renderMarkdown(resp.textContent);
    }

    // 移除 streaming 状态
    resp.container.classList.remove('streaming');
    resp.container.classList.add('complete');

    // 添加完成分隔线
    if (resp.textContent) {
        var divider = document.createElement('div');
        divider.className = 'response-divider';
        divider.innerHTML = '<div class="divider-line"></div>' +
            '<span class="divider-label">回复完成 · 可以继续对话</span>' +
            '<div class="divider-line"></div>';
        resp.container.appendChild(divider);
    }

    // 重置状态
    currentResponse = null;
    document.getElementById('sendBtn').disabled = false;
    loadSkills().then(renderSkillList);
    scrollToBottom();
}

function handleError(data) {
    var resp = currentResponse;
    if (resp) {
        removeLoadingIndicator(resp);
        removeCursor();
        resp.container.classList.remove('streaming');
    }

    var messagesEl = document.getElementById('messages');
    var el = document.createElement('div');
    el.className = 'msg assistant error-msg';
    el.textContent = '错误: ' + (data.content || '未知错误');
    messagesEl.appendChild(el);

    currentResponse = null;
    document.getElementById('sendBtn').disabled = false;
    scrollToBottom();
}

function removeCursor() {
    var cursors = document.querySelectorAll('.streaming-cursor');
    cursors.forEach(function(c) { c.remove(); });
}

// ── Markdown 渲染 ─────────────────────────────────
function renderMarkdown(text) {
    var html = escapeHtml(text);

    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function(m, lang, code) {
        return '<div class="code-block"><div class="code-lang">' + (lang || 'code') + '</div><pre>' + code + '</pre></div>';
    });

    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>');
    html = html.replace(/\n/g, '<br>');

    return html;
}

// ── API ────────────────────────────────────────────
function loadSkills() {
    return fetch('/api/skills').then(function(r) { return r.json(); }).then(function(data) {
        skills = data;
        renderSkillList();
        return data;
    });
}

function loadStatus() {
    fetch('/api/status').then(function(r) { return r.json(); }).then(function(data) {
        var el = document.getElementById('poolStatus');
        if (el) el.textContent = data.active_servers + '/' + data.total_servers + ' 实例';
    }).catch(function() {});
}

function createSkill(message) {
    return fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message }),
    }).then(function(r) { return r.json(); }).then(function(skill) {
        return loadSkills().then(function() {
            selectSkill(skill.id);
            return skill;
        });
    });
}

function loadMessages(skillId) {
    return fetch('/api/skills/' + skillId + '/messages').then(function(r) { return r.json(); });
}

// ── Send Message ───────────────────────────────────
function sendMessage() {
    var ta = document.getElementById('chatInput');
    if (!ta || !activeSkillId) return;
    var text = ta.value.trim();
    if (!text) return;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        connectWS(activeSkillId);
        var waitSend = function() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                doSend(text);
            } else {
                setTimeout(waitSend, 100);
            }
        };
        setTimeout(waitSend, 100);
    } else {
        doSend(text);
    }
}

function doSend(text) {
    var ta = document.getElementById('chatInput');
    ta.value = '';
    ta.style.height = 'auto';
    document.getElementById('sendBtn').disabled = true;

    appendUserMessage(text);
    currentResponse = null;

    ws.send(JSON.stringify({ message: text }));
    _onWsActivity();  // 发送消息，重置WS空闲计时
}

// ── Render ─────────────────────────────────────────
// 状态中文映射
var STATUS_MAP = {
    'creating': '创建中',
    'iterating': '生成中',
    'active': '已完成',
    'completed': '已完成',
    'error': '出错'
};

function statusLabel(status) {
    return STATUS_MAP[status] || status;
}

function renderSkillList() {
    var el = document.getElementById('skillList');
    if (!el) return;
    el.innerHTML = '';
    for (var i = 0; i < skills.length; i++) {
        (function(s) {
            var card = document.createElement('div');
            card.className = 'skill-card' + (s.id === activeSkillId ? ' active' : '');
            card.innerHTML =
                '<div class="card-name">' + escapeHtml(s.name) + '</div>' +
                '<div class="card-meta">' +
                    '<span class="card-status-badge ' + s.status + '">' + statusLabel(s.status) + '</span>' +
                    '<span class="card-msg-count">' + s.message_count + ' 条对话</span>' +
                '</div>';
            card.addEventListener('click', function() { selectSkill(s.id); });
            el.appendChild(card);
        })(skills[i]);
    }
}

function selectSkill(skillId) {
    activeSkillId = skillId;
    var skill = null;
    for (var i = 0; i < skills.length; i++) {
        if (skills[i].id === skillId) { skill = skills[i]; break; }
    }
    if (!skill) return;

    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('chatArea').style.display = 'flex';

    document.getElementById('skillName').textContent = skill.name;

    var statusEl = document.getElementById('skillStatus');
    statusEl.textContent = statusLabel(skill.status);
    statusEl.className = 'chat-skill-status card-status-badge ' + skill.status;

    renderSkillList();
    connectWS(skillId);

    loadMessages(skillId).then(function(msgs) {
        var messagesEl = document.getElementById('messages');
        // 清空前重置状态，防止 WebSocket 写入已删除的 DOM
        currentResponse = null;
        messagesEl.innerHTML = '';
        for (var i = 0; i < msgs.length; i++) {
            if (msgs[i].role === 'user') {
                appendUserMessage(msgs[i].content);
            } else {
                appendAssistantHistory(msgs[i].content, msgs[i].thinking || '');
            }
        }
        scrollToBottom();
    });
}

function appendUserMessage(content) {
    var messagesEl = document.getElementById('messages');
    var el = document.createElement('div');
    el.className = 'msg user';
    el.textContent = content;
    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
}

function appendAssistantHistory(content, thinking) {
    var messagesEl = document.getElementById('messages');
    var container = document.createElement('div');
    container.className = 'ai-response complete';

    // 渲染思考块（如果有），按轮次分隔符拆分为多个块
    if (thinking && thinking.trim()) {
        var rounds = thinking.split('\n\n===THINKING_ROUND===\n\n');
        for (var ri = 0; ri < rounds.length; ri++) {
            var roundText = rounds[ri].trim();
            if (!roundText) continue;

            var block = document.createElement('div');
            block.className = 'thinking-block closed';
            var label = rounds.length > 1
                ? '思考过程 (第 ' + (ri + 1) + '/' + rounds.length + ' 轮)'
                : '思考过程';
            block.innerHTML =
                '<div class="thinking-header" onclick="this.parentElement.classList.toggle(\'open\');this.parentElement.classList.toggle(\'closed\')">' +
                    '<svg class="thinking-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
                    '<span class="thinking-label">' + label + '</span>' +
                    '<svg class="thinking-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>' +
                '</div>' +
                '<div class="thinking-body"></div>';
            block.querySelector('.thinking-body').textContent = roundText;
            container.appendChild(block);
        }
    }

    // 渲染正文
    var textBlock = document.createElement('div');
    textBlock.className = 'response-text';
    textBlock.innerHTML = renderMarkdown(content);
    container.appendChild(textBlock);

    // 分隔线
    var divider = document.createElement('div');
    divider.className = 'response-divider';
    divider.innerHTML = '<div class="divider-line"></div>' +
        '<span class="divider-label">回复完成</span>' +
        '<div class="divider-line"></div>';
    container.appendChild(divider);

    messagesEl.appendChild(container);
    scrollToBottom();
    return container;
}

function scrollToBottom() {
    var el = document.getElementById('messages');
    el.scrollTop = el.scrollHeight;
}

function quickCreate() {
    var input = document.getElementById('quickInput');
    if (!input) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    createSkill(text);
}

function escapeHtml(str) { var d = document.createElement('div'); d.textContent = str; return d.innerHTML; }
