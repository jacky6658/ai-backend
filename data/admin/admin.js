// 根據部署環境自動選擇 API 基礎 URL
const API_BASE = window.location.hostname === 'localhost' 
  ? 'http://localhost:8080'
  : 'https://aijobvideobackend.zeabur.app';

// 全域變數
let currentTab = 'users';
let currentPage = 1;
let pageSize = 10;
let totalPages = 1;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
  checkAdminAuth();
  initTabs();
  loadStats();
  loadUsers();
});

// 檢查管理員權限
function checkAdminAuth() {
  const isLoggedIn = localStorage.getItem('isLoggedIn');
  const user = JSON.parse(localStorage.getItem('user') || '{}');
  
  // 這裡應該檢查用戶是否有管理員權限
  // 暫時允許所有登入用戶訪問後台
  if (isLoggedIn !== 'true') {
    alert('請先登入');
    window.location.href = 'login.html';
    return;
  }
  
  console.log('管理員登入:', user);
}

// 初始化標籤頁
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => {
      // 移除所有active類別
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      
      // 添加active類別到點擊的按鈕和對應內容
      btn.classList.add('active');
      const tabId = btn.dataset.tab;
      document.getElementById(tabId + '-tab').classList.add('active');
      
      currentTab = tabId;
      loadTabData();
    };
  });
}

// 載入標籤頁數據
function loadTabData() {
  switch(currentTab) {
    case 'users':
      loadUsers();
      break;
    case 'scripts':
      loadScripts();
      break;
    case 'topics':
      loadTopics();
      break;
    case 'positioning':
      loadPositioning();
      break;
    case 'analytics':
      loadAnalytics();
      break;
  }
}

// 載入統計數據
async function loadStats() {
  try {
    // 模擬API調用
    const stats = {
      totalUsers: 1250,
      activeUsers: 890,
      totalScripts: 3420,
      totalTopics: 2100
    };
    
    document.getElementById('totalUsers').textContent = stats.totalUsers.toLocaleString();
    document.getElementById('activeUsers').textContent = stats.activeUsers.toLocaleString();
    document.getElementById('totalScripts').textContent = stats.totalScripts.toLocaleString();
    document.getElementById('totalTopics').textContent = stats.totalTopics.toLocaleString();
    
  } catch (error) {
    console.error('載入統計數據失敗:', error);
  }
}

// 載入用戶列表
async function loadUsers(page = 1) {
  const tbody = document.getElementById('usersTableBody');
  tbody.innerHTML = '<tr><td colspan="6" class="loading">載入中...</td></tr>';
  
  try {
    // 模擬API調用
    const mockUsers = generateMockUsers(50);
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const pageUsers = mockUsers.slice(startIndex, endIndex);
    
    tbody.innerHTML = '';
    pageUsers.forEach(user => {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <img src="${user.avatar}" class="user-avatar" alt="${user.name}">
            <div>
              <div style="font-weight:600;">${user.name}</div>
              <div style="font-size:12px; color:var(--text-muted);">${user.email}</div>
            </div>
          </div>
        </td>
        <td>
          <span style="padding:4px 8px; border-radius:4px; font-size:12px; background:${user.provider === 'google' ? '#4285f4' : '#00c300'}; color:white;">
            ${user.provider.toUpperCase()}
          </span>
        </td>
        <td>${formatDate(user.createdAt)}</td>
        <td>${formatDate(user.lastActive)}</td>
        <td>
          <span class="status-badge ${user.isActive ? 'status-active' : 'status-inactive'}">
            ${user.isActive ? '活躍' : '非活躍'}
          </span>
        </td>
        <td>
          <button class="btn secondary" onclick="viewUserDetail('${user.id}')">查看</button>
          <button class="btn danger" onclick="deleteUser('${user.id}')">刪除</button>
        </td>
      `;
      tbody.appendChild(row);
    });
    
    updatePagination('usersPagination', page, Math.ceil(mockUsers.length / pageSize));
    
  } catch (error) {
    console.error('載入用戶列表失敗:', error);
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">載入失敗</td></tr>';
  }
}

// 載入腳本列表
async function loadScripts(page = 1) {
  const tbody = document.getElementById('scriptsTableBody');
  tbody.innerHTML = '<tr><td colspan="6" class="loading">載入中...</td></tr>';
  
  try {
    // 模擬API調用
    const mockScripts = generateMockScripts(100);
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const pageScripts = mockScripts.slice(startIndex, endIndex);
    
    tbody.innerHTML = '';
    pageScripts.forEach(script => {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <img src="${script.userAvatar}" class="user-avatar" alt="${script.userName}">
            <span style="font-weight:600;">${script.userName}</span>
          </div>
        </td>
        <td>
          <div style="max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
            ${script.content}
          </div>
        </td>
        <td>${script.templateType}</td>
        <td>${script.duration}秒</td>
        <td>${formatDate(script.createdAt)}</td>
        <td>
          <button class="btn secondary" onclick="viewScriptDetail('${script.id}')">查看</button>
          <button class="btn danger" onclick="deleteScript('${script.id}')">刪除</button>
        </td>
      `;
      tbody.appendChild(row);
    });
    
    updatePagination('scriptsPagination', page, Math.ceil(mockScripts.length / pageSize));
    
  } catch (error) {
    console.error('載入腳本列表失敗:', error);
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">載入失敗</td></tr>';
  }
}

// 載入選題列表
async function loadTopics(page = 1) {
  const tbody = document.getElementById('topicsTableBody');
  tbody.innerHTML = '<tr><td colspan="5" class="loading">載入中...</td></tr>';
  
  try {
    // 模擬API調用
    const mockTopics = generateMockTopics(80);
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const pageTopics = mockTopics.slice(startIndex, endIndex);
    
    tbody.innerHTML = '';
    pageTopics.forEach(topic => {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <img src="${topic.userAvatar}" class="user-avatar" alt="${topic.userName}">
            <span style="font-weight:600;">${topic.userName}</span>
          </div>
        </td>
        <td>
          <span style="padding:4px 8px; border-radius:4px; font-size:12px; background:#e3f2fd; color:#1976d2;">
            ${topic.type}
          </span>
        </td>
        <td>
          <div style="max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
            ${topic.content}
          </div>
        </td>
        <td>${formatDate(topic.createdAt)}</td>
        <td>
          <button class="btn secondary" onclick="viewTopicDetail('${topic.id}')">查看</button>
          <button class="btn danger" onclick="deleteTopic('${topic.id}')">刪除</button>
        </td>
      `;
      tbody.appendChild(row);
    });
    
    updatePagination('topicsPagination', page, Math.ceil(mockTopics.length / pageSize));
    
  } catch (error) {
    console.error('載入選題列表失敗:', error);
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">載入失敗</td></tr>';
  }
}

// 載入定位列表
async function loadPositioning(page = 1) {
  const tbody = document.getElementById('positioningTableBody');
  tbody.innerHTML = '<tr><td colspan="7" class="loading">載入中...</td></tr>';
  
  try {
    // 模擬API調用
    const mockPositioning = generateMockPositioning(60);
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const pagePositioning = mockPositioning.slice(startIndex, endIndex);
    
    tbody.innerHTML = '';
    pagePositioning.forEach(pos => {
      const completionRate = Math.round((pos.completedFields / pos.totalFields) * 100);
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <img src="${pos.userAvatar}" class="user-avatar" alt="${pos.userName}">
            <span style="font-weight:600;">${pos.userName}</span>
          </div>
        </td>
        <td>${pos.businessType || '未設定'}</td>
        <td>${pos.targetAudience || '未設定'}</td>
        <td>${pos.brandVoice || '未設定'}</td>
        <td>${pos.primaryPlatform || '未設定'}</td>
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <div style="width:60px; height:8px; background:#e5e7eb; border-radius:4px; overflow:hidden;">
              <div style="width:${completionRate}%; height:100%; background:${completionRate >= 80 ? '#16a34a' : '#f59e0b'}; transition:all 0.3s;"></div>
            </div>
            <span style="font-size:12px; font-weight:600;">${completionRate}%</span>
          </div>
        </td>
        <td>
          <button class="btn secondary" onclick="viewPositioningDetail('${pos.id}')">查看</button>
          <button class="btn danger" onclick="deletePositioning('${pos.id}')">刪除</button>
        </td>
      `;
      tbody.appendChild(row);
    });
    
    updatePagination('positioningPagination', page, Math.ceil(mockPositioning.length / pageSize));
    
  } catch (error) {
    console.error('載入定位列表失敗:', error);
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">載入失敗</td></tr>';
  }
}

// 載入分析數據
async function loadAnalytics() {
  const chartDiv = document.getElementById('analyticsChart');
  chartDiv.innerHTML = `
    <div style="text-align:center;">
      <div style="font-size:48px; margin-bottom:16px;">📊</div>
      <h3>使用統計圖表</h3>
      <p>這裡將顯示用戶使用趨勢、熱門功能等統計圖表</p>
      <p style="font-size:14px; color:var(--text-muted);">需要整合圖表庫（如Chart.js）來顯示詳細數據</p>
    </div>
  `;
}

// 更新分頁
function updatePagination(containerId, currentPage, totalPages) {
  const container = document.getElementById(containerId);
  if (!container) return;
  
  let html = '';
  
  // 上一頁按鈕
  html += `<button class="page-btn" ${currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${currentPage - 1})">上一頁</button>`;
  
  // 頁碼按鈕
  const startPage = Math.max(1, currentPage - 2);
  const endPage = Math.min(totalPages, currentPage + 2);
  
  if (startPage > 1) {
    html += `<button class="page-btn" onclick="goToPage(1)">1</button>`;
    if (startPage > 2) {
      html += `<span style="padding:8px;">...</span>`;
    }
  }
  
  for (let i = startPage; i <= endPage; i++) {
    html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`;
  }
  
  if (endPage < totalPages) {
    if (endPage < totalPages - 1) {
      html += `<span style="padding:8px;">...</span>`;
    }
    html += `<button class="page-btn" onclick="goToPage(${totalPages})">${totalPages}</button>`;
  }
  
  // 下一頁按鈕
  html += `<button class="page-btn" ${currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${currentPage + 1})">下一頁</button>`;
  
  container.innerHTML = html;
}

// 跳轉頁面
function goToPage(page) {
  currentPage = page;
  loadTabData();
}

// 搜尋功能
function searchUsers() {
  const searchTerm = document.getElementById('userSearch').value;
  const filter = document.getElementById('userFilter').value;
  console.log('搜尋用戶:', searchTerm, filter);
  loadUsers();
}

function searchScripts() {
  const searchTerm = document.getElementById('scriptSearch').value;
  const filter = document.getElementById('scriptFilter').value;
  console.log('搜尋腳本:', searchTerm, filter);
  loadScripts();
}

function searchTopics() {
  const searchTerm = document.getElementById('topicSearch').value;
  const filter = document.getElementById('topicFilter').value;
  console.log('搜尋選題:', searchTerm, filter);
  loadTopics();
}

function searchPositioning() {
  const searchTerm = document.getElementById('positioningSearch').value;
  const filter = document.getElementById('positioningFilter').value;
  console.log('搜尋定位:', searchTerm, filter);
  loadPositioning();
}

// 匯出功能
function exportUsers() {
  console.log('匯出用戶資料');
  downloadFile('users.csv', generateCSV(generateMockUsers(50)));
}

function exportScripts() {
  console.log('匯出腳本資料');
  downloadFile('scripts.json', JSON.stringify(generateMockScripts(100), null, 2));
}

function exportTopics() {
  console.log('匯出選題資料');
  downloadFile('topics.json', JSON.stringify(generateMockTopics(80), null, 2));
}

function exportPositioning() {
  console.log('匯出定位資料');
  downloadFile('positioning.json', JSON.stringify(generateMockPositioning(60), null, 2));
}

function exportAnalytics() {
  console.log('匯出分析資料');
  const analyticsData = {
    totalUsers: 1250,
    activeUsers: 890,
    totalScripts: 3420,
    totalTopics: 2100,
    generatedAt: new Date().toISOString()
  };
  downloadFile('analytics.csv', generateCSV([analyticsData]));
}

function exportAllData() {
  console.log('匯出全部資料');
  const allData = {
    users: generateMockUsers(50),
    scripts: generateMockScripts(100),
    topics: generateMockTopics(80),
    positioning: generateMockPositioning(60),
    analytics: {
      totalUsers: 1250,
      activeUsers: 890,
      totalScripts: 3420,
      totalTopics: 2100
    },
    exportedAt: new Date().toISOString()
  };
  downloadFile('all_data.json', JSON.stringify(allData, null, 2));
}

// 下載文件
function downloadFile(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// 生成CSV
function generateCSV(data) {
  if (!data.length) return '';
  
  const headers = Object.keys(data[0]);
  const csvContent = [
    headers.join(','),
    ...data.map(row => headers.map(header => `"${row[header] || ''}"`).join(','))
  ].join('\n');
  
  return csvContent;
}

// 工具函數
function formatDate(dateString) {
  return new Date(dateString).toLocaleDateString('zh-TW', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

// 登出
function logout() {
  if (confirm('確定要登出嗎？')) {
    localStorage.removeItem('user');
    localStorage.removeItem('isLoggedIn');
    window.location.href = 'login.html';
  }
}

// 重新整理數據
function refreshData() {
  loadStats();
  loadTabData();
}

// 查看詳情（模擬）
function viewUserDetail(userId) {
  alert(`查看用戶詳情: ${userId}`);
}

function viewScriptDetail(scriptId) {
  alert(`查看腳本詳情: ${scriptId}`);
}

function viewTopicDetail(topicId) {
  alert(`查看選題詳情: ${topicId}`);
}

function viewPositioningDetail(positioningId) {
  alert(`查看定位詳情: ${positioningId}`);
}

// 刪除功能（模擬）
function deleteUser(userId) {
  if (confirm('確定要刪除這個用戶嗎？')) {
    console.log('刪除用戶:', userId);
    alert('用戶已刪除');
    loadUsers(currentPage);
  }
}

function deleteScript(scriptId) {
  if (confirm('確定要刪除這個腳本嗎？')) {
    console.log('刪除腳本:', scriptId);
    alert('腳本已刪除');
    loadScripts(currentPage);
  }
}

function deleteTopic(topicId) {
  if (confirm('確定要刪除這個選題嗎？')) {
    console.log('刪除選題:', topicId);
    alert('選題已刪除');
    loadTopics(currentPage);
  }
}

function deletePositioning(positioningId) {
  if (confirm('確定要刪除這個定位嗎？')) {
    console.log('刪除定位:', positioningId);
    alert('定位已刪除');
    loadPositioning(currentPage);
  }
}

// 生成模擬數據
function generateMockUsers(count) {
  const users = [];
  const providers = ['google', 'line'];
  const names = ['張小明', '李小華', '王大強', '陳小美', '林小芳', '黃小偉', '劉小玲', '吳小傑'];
  
  for (let i = 0; i < count; i++) {
    const provider = providers[Math.floor(Math.random() * providers.length)];
    const name = names[Math.floor(Math.random() * names.length)] + (i + 1);
    users.push({
      id: `user_${i + 1}`,
      name: name,
      email: `user${i + 1}@${provider === 'google' ? 'gmail.com' : 'line.com'}`,
      provider: provider,
      avatar: `https://via.placeholder.com/40?text=${name.charAt(0)}`,
      createdAt: new Date(Date.now() - Math.random() * 30 * 24 * 60 * 60 * 1000).toISOString(),
      lastActive: new Date(Date.now() - Math.random() * 7 * 24 * 60 * 60 * 1000).toISOString(),
      isActive: Math.random() > 0.3
    });
  }
  
  return users;
}

function generateMockScripts(count) {
  const scripts = [];
  const templates = ['A 三段式', 'B 問題解決', 'C Before-After', 'D 教學', 'E 敘事', 'F 爆點連發'];
  const durations = [30, 60];
  const contents = [
    '今天要分享一個超實用的技巧，讓你在30秒內學會這個方法...',
    '你是不是也遇到過這樣的問題？讓我來教你解決方案...',
    '之前我是這樣做的，但現在我發現了更好的方法...',
    '很多人不知道，其實這個技巧非常簡單，只需要三個步驟...'
  ];
  
  for (let i = 0; i < count; i++) {
    scripts.push({
      id: `script_${i + 1}`,
      userId: `user_${Math.floor(Math.random() * 50) + 1}`,
      userName: `用戶${i + 1}`,
      userAvatar: `https://via.placeholder.com/40?text=U${i + 1}`,
      content: contents[Math.floor(Math.random() * contents.length)],
      templateType: templates[Math.floor(Math.random() * templates.length)],
      duration: durations[Math.floor(Math.random() * durations.length)],
      createdAt: new Date(Date.now() - Math.random() * 30 * 24 * 60 * 60 * 1000).toISOString()
    });
  }
  
  return scripts;
}

function generateMockTopics(count) {
  const topics = [];
  const types = ['熱門趨勢', '教育分享', '個人故事', '產品介紹'];
  const contents = [
    '蹭「生活痛點/共鳴」熱點 ✨ (例如：年輕人的消費觀、職場困境、社交焦慮)',
    '揭秘式選題：激發觀眾的「窺視慾」「避坑心理」',
    '抓住人性的劣根（貪婪、好奇、虛榮、懶惰）',
    '滿足用戶的幻想（美好關係、美好人生、生活切片素材）'
  ];
  
  for (let i = 0; i < count; i++) {
    topics.push({
      id: `topic_${i + 1}`,
      userId: `user_${Math.floor(Math.random() * 50) + 1}`,
      userName: `用戶${i + 1}`,
      userAvatar: `https://via.placeholder.com/40?text=U${i + 1}`,
      type: types[Math.floor(Math.random() * types.length)],
      content: contents[Math.floor(Math.random() * contents.length)],
      createdAt: new Date(Date.now() - Math.random() * 30 * 24 * 60 * 60 * 1000).toISOString()
    });
  }
  
  return topics;
}

function generateMockPositioning(count) {
  const positioning = [];
  const businessTypes = ['AI智能體', '電商', '教育', '餐飲', '健身', '美妝', '科技', '金融'];
  const targetAudiences = ['年輕上班族', '學生族群', '家庭主婦', '創業者', '企業主'];
  const brandVoices = ['專業權威', '親切友善', '活潑有趣', '沉穩可靠', '創新前衛'];
  const platforms = ['Instagram', 'TikTok', 'YouTube', 'Facebook', '小紅書'];
  
  for (let i = 0; i < count; i++) {
    const completedFields = Math.floor(Math.random() * 6);
    positioning.push({
      id: `positioning_${i + 1}`,
      userId: `user_${Math.floor(Math.random() * 50) + 1}`,
      userName: `用戶${i + 1}`,
      userAvatar: `https://via.placeholder.com/40?text=U${i + 1}`,
      businessType: completedFields > 0 ? businessTypes[Math.floor(Math.random() * businessTypes.length)] : null,
      targetAudience: completedFields > 1 ? targetAudiences[Math.floor(Math.random() * targetAudiences.length)] : null,
      brandVoice: completedFields > 2 ? brandVoices[Math.floor(Math.random() * brandVoices.length)] : null,
      primaryPlatform: completedFields > 3 ? platforms[Math.floor(Math.random() * platforms.length)] : null,
      completedFields: completedFields,
      totalFields: 6,
      createdAt: new Date(Date.now() - Math.random() * 30 * 24 * 60 * 60 * 1000).toISOString()
    });
  }
  
  return positioning;
}
