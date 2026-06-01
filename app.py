# 这是后端主服务，负责简历管理、AI 面试、报告生成和本地 SQLite 数据存储。
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import uuid
import copy
from datetime import datetime
import json
import re
import sqlite3
from openai import OpenAI
import fitz  # PyMuPDF
from PIL import Image
import io
from resume_generator import generate_resume_pdf
from interview_report_pdf import generate_interview_report_pdf

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 配置
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB限制

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# 确保目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
GENERATED_RESUMES_FOLDER = 'generated_resumes'
os.makedirs(GENERATED_RESUMES_FOLDER, exist_ok=True)

# 本地 SQLite 数据库
DATABASE_FILE = os.path.join(BASE_DIR, 'resumevault.sqlite3')

# DeepSeek API 配置
DEEPSEEK_API_KEY = "sk-af7f39e6bcc3469595760639ff4290f1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 初始化 DeepSeek 客户端
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

# OCR引擎（延迟加载）
ocr_engine = None

def get_db_connection():
    """连接本地 SQLite 数据库"""
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection

def initialize_database():
    """初始化系统所有本地数据表"""
    with get_db_connection() as connection:
        connection.execute('''
            CREATE TABLE IF NOT EXISTS app_data (
                data_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS job_records (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS favorite_jobs (
                job_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS candidate_favorite_jobs (
                resume_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                candidate_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (resume_id, job_id)
            )
        ''')

def load_app_data(data_key, default_value):
    """从 SQLite 读取一类系统数据，不存在时初始化默认值"""
    initialize_database()
    with get_db_connection() as connection:
        row = connection.execute(
            'SELECT payload FROM app_data WHERE data_key = ?',
            (data_key,)
        ).fetchone()

    if row:
        try:
            return json.loads(row['payload'])
        except json.JSONDecodeError as e:
            print(f"SQLite 数据格式错误，已回退为空数据: {e}")
            return default_value

    save_app_data(data_key, default_value)
    return default_value

def save_app_data(data_key, data):
    """把一类系统数据保存到 SQLite"""
    initialize_database()
    payload = json.dumps(data, ensure_ascii=False)
    now = datetime.now().isoformat()
    with get_db_connection() as connection:
        connection.execute(
            '''
            INSERT OR REPLACE INTO app_data (data_key, payload, updated_at)
            VALUES (?, ?, ?)
            ''',
            (data_key, payload, now)
        )

def get_ocr_engine():
    """获取OCR引擎（延迟加载）"""
    global ocr_engine
    if ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            ocr_engine = RapidOCR()
            print("OCR引擎初始化成功")
        except Exception as e:
            print(f"OCR引擎初始化失败: {e}")
            ocr_engine = "failed"
    return ocr_engine if ocr_engine != "failed" else None

def load_metadata():
    """从 SQLite 加载简历元数据"""
    data = load_app_data('resumes_metadata', [])
    if isinstance(data, list):
        return data
    print("元数据格式错误：resumes_metadata 不是数组，已回退为空列表")
    return []

def save_metadata(data):
    """保存简历元数据到 SQLite"""
    save_app_data('resumes_metadata', data)

def load_resume_contents():
    """从 SQLite 加载简历内容缓存"""
    data = load_app_data('resume_contents', {})
    return data if isinstance(data, dict) else {}

def save_resume_contents(data):
    """保存简历内容缓存到 SQLite"""
    save_app_data('resume_contents', data)

def load_interview_sessions():
    """从 SQLite 加载面试会话"""
    data = load_app_data('interview_sessions', {})
    return data if isinstance(data, dict) else {}

def save_interview_sessions(data):
    """保存面试会话到 SQLite"""
    save_app_data('interview_sessions', data)

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_file_size_mb(file_path):
    """获取文件大小（MB）"""
    return os.path.getsize(file_path) / (1024 * 1024)

def extract_text_from_pdf(pdf_path):
    """从PDF中提取文本内容
    
    首先尝试直接提取文本，如果文本很少则使用OCR
    """
    try:
        doc = fitz.open(pdf_path)
        
        # 首先尝试直接提取文本
        direct_text = ""
        for page in doc:
            direct_text += page.get_text()
        
        # 如果直接提取的文本足够多（超过100个字符），说明不是纯图片PDF
        if len(direct_text.strip()) > 100:
            doc.close()
            return direct_text.strip(), "direct"
        
        # 否则使用OCR
        ocr = get_ocr_engine()
        if ocr is None:
            doc.close()
            return direct_text.strip() if direct_text.strip() else "无法提取简历内容", "failed"
        
        ocr_text = ""
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # 将页面渲染为图片（提高分辨率以获得更好的OCR效果）
            mat = fitz.Matrix(2.0, 2.0)  # 2倍缩放
            pix = page.get_pixmap(matrix=mat)
            
            # 转换为PIL Image
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            
            # 使用OCR识别
            result, _ = ocr(img)
            
            if result:
                page_text = "\n".join([line[1] for line in result])
                ocr_text += f"\n--- 第{page_num + 1}页 ---\n{page_text}"
        
        doc.close()
        
        final_text = ocr_text.strip() if ocr_text.strip() else direct_text.strip()
        return final_text if final_text else "无法提取简历内容", "ocr"
        
    except Exception as e:
        print(f"PDF提取错误: {e}")
        return f"提取失败: {str(e)}", "error"

def is_valid_resume_content(content):
    """判断简历内容是否有效，可用于后续AI解析"""
    return bool(content) and content not in ["无法提取简历内容", ""] and not str(content).startswith("提取失败")

def parse_resume_key_info(content):
    """解析简历关键信息（姓名、岗位、技能等）"""
    parsed_info = {}
    if not is_valid_resume_content(content):
        return parsed_info

    try:
        parse_prompt = f"""请仔细分析以下简历内容，提取关键信息。

## 简历原文
{content[:5000]}

---

请提取以下信息，严格按照JSON格式返回（不要添加任何其他内容）：
{{
    "candidateName": "候选人姓名（必填，从简历中识别）",
    "position": "应聘职位/目标岗位（如果简历中没有明确写，根据技能和经验推断最匹配的职位）",
    "phone": "手机号码",
    "email": "邮箱地址",
    "education": {{
        "school": "最高学历学校",
        "major": "专业",
        "degree": "学历（本科/硕士/博士等）",
        "graduationYear": "毕业年份"
    }},
    "workYears": "工作年限（如：3年经验、应届生）",
    "currentCompany": "当前/最近公司",
    "currentPosition": "当前/最近职位",
    "skills": ["技能1", "技能2", "技能3"],
    "highlights": [
        "简历亮点1（如核心项目、获奖经历、技术特长等）",
        "简历亮点2"
    ],
    "summary": "一句话总结候选人背景（30字以内）"
}}

要求：
1. candidateName 必须提取，这是最重要的字段
2. position 如果简历未明确写，可根据技能和经验推断（如：Java后端开发、前端工程师等）
3. skills 提取3-8个核心技能
4. highlights 提取2-3个最大亮点，用于面试重点关注
5. 如果某项信息确实无法识别，使用空字符串""或空数组[]"""

        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位专业的HR简历解析专家。请从简历中准确提取关键信息，严格按照JSON格式返回。"},
                {"role": "user", "content": parse_prompt}
            ],
            stream=False
        )

        parse_text = response.choices[0].message.content
        json_match = re.search(r'\{[\s\S]*\}', parse_text)
        if json_match:
            parsed_info = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"简历解析JSON错误: {e}")
    except Exception as e:
        print(f"简历解析错误: {e}")

    return parsed_info

def extract_structured_resume_data(content, candidate_name="", position=""):
    """从简历文本中提取结构化简历数据"""
    if not is_valid_resume_content(content):
        return None

    extraction_prompt = f"""请从以下简历内容中提取结构化信息，严格按照JSON格式返回：

【简历内容】
{content[:4000]}

【候选人信息】
姓名: {candidate_name}
应聘职位: {position}

请严格按照以下JSON格式返回（不要添加任何其他内容）：
{{
    "motto": "个人格言（如无法提取则留空）",
    "personalSummary": "个人优势摘要（如无法提取则留空）",
    "basicInfo": {{
        "姓名": "{candidate_name}",
        "性别": "男/女（如无法确定则留空）",
        "年龄": "",
        "籍贯": "",
        "工作年限": "",
        "电话": "",
        "邮箱": ""
    }},
    "jobIntention": {{
        "职位": "{position}",
        "城市": "",
        "期望薪资": "",
        "到岗": ""
    }},
    "education": {{
        "时间": "",
        "学校": "",
        "专业": "",
        "专业成绩": "",
        "主修课程": ""
    }},
    "workExperience": [
        {{
            "period": "时间段",
            "company": "公司名称",
            "position": "职位",
            "responsibilities": ["职责1", "职责2"]
        }}
    ],
    "projects": [
        {{
            "projectName": "项目名称",
            "period": "时间段",
            "position": "担任角色",
            "description": "项目描述",
            "responsibilities": ["职责1", "职责2"]
        }}
    ],
    "skills": {{
        "技能名称": {{"description": "技能描述", "level": 80}}
    }},
    "certificates": ["证书1", "证书2"],
    "selfEvaluation": "自我评价"
}}

如果某些信息无法从简历中提取，请使用空字符串或空数组。"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位专业的简历解析专家。请从简历内容中提取结构化信息，严格按照JSON格式返回。"},
                {"role": "user", "content": extraction_prompt}
            ],
            stream=False
        )
        extracted_text = response.choices[0].message.content
        json_match = re.search(r'\{[\s\S]*\}', extracted_text)
        if json_match:
            return json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"结构化数据JSON解析失败: {e}")
    except Exception as e:
        print(f"AI提取结构化数据错误: {e}")

    return None

def extract_and_cache_resume_data(file_id):
    """提取并缓存简历数据（文本、关键信息、结构化编辑数据）"""
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id and r.get('status') == 'active'), None)
    if not resume:
        raise ValueError('简历不存在')

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], resume['filename'])
    if not os.path.exists(file_path):
        raise FileNotFoundError('文件不存在')

    # 1) 提取简历原始文本
    content, method = extract_text_from_pdf(file_path)

    # 2) 解析关键信息
    parsed_info = parse_resume_key_info(content)

    # 3) 缓存简历内容与解析信息
    contents = load_resume_contents()
    contents[file_id] = {
        'content': content,
        'method': method,
        'extractTime': datetime.now().isoformat(),
        'parsedInfo': parsed_info
    }
    save_resume_contents(contents)

    # 4) 回写简历元数据
    for item in metadata:
        if item['id'] == file_id:
            item['contentExtracted'] = True
            if parsed_info.get('candidateName') and item.get('candidateName') in ['未知', '', None]:
                item['candidateName'] = parsed_info['candidateName']
            if parsed_info.get('position') and item.get('position') in ['未知', '', None]:
                item['position'] = parsed_info['position']
            item['parsedInfo'] = {
                'phone': parsed_info.get('phone', ''),
                'email': parsed_info.get('email', ''),
                'education': parsed_info.get('education', {}),
                'workYears': parsed_info.get('workYears', ''),
                'currentCompany': parsed_info.get('currentCompany', ''),
                'currentPosition': parsed_info.get('currentPosition', ''),
                'skills': parsed_info.get('skills', []),
                'highlights': parsed_info.get('highlights', []),
                'summary': parsed_info.get('summary', '')
            }
            resume = item
            break
    save_metadata(metadata)

    # 5) 预生成手动编辑所需结构化数据
    structured_saved = False
    structured_data = load_structured_data()
    if file_id not in structured_data:
        structured_result = extract_structured_resume_data(
            content,
            parsed_info.get('candidateName') or resume.get('candidateName', ''),
            parsed_info.get('position') or resume.get('position', '')
        )
        if structured_result:
            structured_data[file_id] = structured_result
            save_structured_data(structured_data)
            structured_saved = True

    return {
        'content': content,
        'method': method,
        'parsedInfo': parsed_info,
        'structuredSaved': structured_saved
    }

def detect_position_type(position):
    """根据职位名称识别岗位类型"""
    position_lower = position.lower()
    
    # 后端开发
    if any(keyword in position_lower for keyword in ['后端', 'backend', 'java', 'python', 'go', 'golang', 'c++', '服务端', '服务器', 'node']):
        return 'backend'
    # 前端开发
    elif any(keyword in position_lower for keyword in ['前端', 'frontend', 'web', 'react', 'vue', 'angular', 'javascript', 'typescript', 'h5']):
        return 'frontend'
    # 全栈开发
    elif any(keyword in position_lower for keyword in ['全栈', 'fullstack', 'full-stack', 'full stack']):
        return 'fullstack'
    # 算法/机器学习
    elif any(keyword in position_lower for keyword in ['算法', 'algorithm', 'ai', '机器学习', 'ml', '深度学习', 'nlp', 'cv', '推荐', '搜索']):
        return 'algorithm'
    # 测试
    elif any(keyword in position_lower for keyword in ['测试', 'test', 'qa', '质量']):
        return 'test'
    # 运维/DevOps
    elif any(keyword in position_lower for keyword in ['运维', 'devops', 'sre', '部署', '云']):
        return 'devops'
    # 移动端
    elif any(keyword in position_lower for keyword in ['android', 'ios', '移动', 'flutter', 'react native', '客户端']):
        return 'mobile'
    # 数据相关
    elif any(keyword in position_lower for keyword in ['数据', 'data', 'etl', '仓库', 'bi', '分析']):
        return 'data'
    # 默认通用开发
    else:
        return 'general'


def get_position_focus_areas(position_type):
    """根据岗位类型返回面试重点领域"""
    focus_areas = {
        'backend': {
            'name': '后端开发',
            'technical_basics': [
                '数据结构与算法（链表、树、图、哈希表、排序、动态规划）',
                '操作系统（进程/线程、内存管理、锁机制、死锁）',
                '计算机网络（TCP/IP、HTTP/HTTPS、三次握手/四次挥手、WebSocket）',
                '数据库（MySQL索引原理、事务ACID、隔离级别、慢查询优化、分库分表）',
                '缓存（Redis数据结构、持久化、集群、过期策略）',
                '消息队列（Kafka/RabbitMQ原理、消息可靠性、顺序性）'
            ],
            'system_design': [
                '微服务架构设计',
                '分布式系统（CAP理论、一致性协议）',
                '高并发系统设计（秒杀、限流、降级）',
                '数据库分库分表方案',
                '缓存设计与缓存一致性'
            ],
            'coding': ['实现LRU缓存', '设计线程安全的单例', '手写生产者消费者模型'],
            'engineering': ['Git工作流', 'CI/CD流程', '单元测试/集成测试', '日志与监控', '性能调优']
        },
        'frontend': {
            'name': '前端开发',
            'technical_basics': [
                'HTML/CSS基础（盒模型、Flex/Grid布局、BFC）',
                'JavaScript核心（原型链、闭包、事件循环、Promise/async-await）',
                '浏览器原理（渲染流程、重绘回流、性能优化）',
                'React/Vue框架原理（虚拟DOM、Diff算法、状态管理）',
                'TypeScript（类型系统、泛型）',
                'HTTP协议与网络请求（跨域、缓存策略）'
            ],
            'system_design': [
                '前端架构设计（组件化、模块化）',
                '状态管理方案选型',
                '性能优化策略（首屏加载、懒加载）',
                '微前端架构',
                'SSR/SSG方案'
            ],
            'coding': ['实现防抖节流', '手写Promise', 'Virtual DOM简易实现'],
            'engineering': ['Webpack/Vite构建工具', '代码规范与ESLint', '前端监控与埋点', '自动化测试']
        },
        'algorithm': {
            'name': '算法工程师',
            'technical_basics': [
                '机器学习基础（监督/非监督学习、过拟合/欠拟合）',
                '深度学习框架（TensorFlow/PyTorch）',
                '常见模型（CNN、RNN、Transformer、BERT）',
                '模型训练与调优（学习率、正则化、Batch Normalization）',
                '特征工程与数据预处理',
                '模型评估指标（AUC、F1、召回率）'
            ],
            'system_design': [
                '推荐系统架构',
                '搜索排序系统',
                '模型在线服务化',
                '特征平台设计',
                'A/B测试系统'
            ],
            'coding': ['实现常见算法', '模型代码调试', '数据处理Pipeline'],
            'engineering': ['模型版本管理', 'MLOps实践', '模型监控与迭代']
        },
        'test': {
            'name': '测试工程师',
            'technical_basics': [
                '测试理论（黑盒/白盒测试、边界值分析）',
                '测试用例设计方法',
                '自动化测试框架（Selenium、Appium、Pytest）',
                '接口测试（Postman、JMeter）',
                '性能测试基础',
                'SQL与数据库验证'
            ],
            'system_design': [
                '测试策略制定',
                '自动化测试架构',
                '持续集成中的测试',
                '测试平台设计'
            ],
            'coding': ['自动化脚本编写', '测试框架使用'],
            'engineering': ['缺陷管理', '测试报告', 'CI集成']
        },
        'general': {
            'name': '软件开发',
            'technical_basics': [
                '数据结构与算法基础',
                '编程语言核心概念',
                '操作系统基础',
                '计算机网络基础',
                '数据库基础'
            ],
            'system_design': [
                '基础架构设计能力',
                '模块划分与接口设计',
                '常见设计模式'
            ],
            'coding': ['基础算法实现', '代码规范'],
            'engineering': ['版本控制', '基础开发流程']
        }
    }
    
    # 其他类型使用通用配置
    return focus_areas.get(position_type, focus_areas['general'])

def parse_score_value(raw_score, default=75):
    """将评分值转换为整数，兼容字符串格式"""
    if raw_score is None:
        return default

    try:
        return int(raw_score)
    except (TypeError, ValueError):
        if isinstance(raw_score, str):
            match = re.search(r'\d+', raw_score)
            if match:
                try:
                    return int(match.group(0))
                except ValueError:
                    return default
    return default

def normalize_text_list(value):
    """把字符串或列表统一成文本列表"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r'\n+', value) if item.strip()]
    return []

def normalize_match_text(value):
    """整理用于岗位匹配的文本"""
    if isinstance(value, list):
        return ' '.join(normalize_match_text(item) for item in value)
    if isinstance(value, dict):
        return ' '.join(normalize_match_text(item) for item in value.values())
    return str(value or '').lower()

def extract_match_terms(*values):
    """从简历和岗位文本中提取匹配词"""
    text = normalize_match_text(list(values))
    raw_terms = re.split(r'[\s,，、/|;；:：()（）\[\]【】{}<>《》。.!?？\-]+', text)
    terms = {term.strip().lower() for term in raw_terms if len(term.strip()) >= 2}
    important_terms = [
        'c', 'c++', 'python', 'java', 'javascript', 'typescript', 'vue', 'react',
        'node', 'flask', 'django', 'spring', 'mysql', 'sqlite', 'redis', 'linux', 'stm32', 'gd32', 'usb', 'gpio',
        'pcb', 'eda', 'mcu', 'can', 'uart', 'spi', 'i2c', 'ethernet', 'opencv',
        'pytorch', 'tensorflow', '深度学习', '机器学习', '算法', '通信', '电子',
        '自动化', '硬件', '嵌入式', '单片机', '音频', '编解码', '机器人',
        '会计', '财务', '出纳', '税务', '审计', '成本', '报表', '金蝶', '用友',
        '测试', '运营', '销售', '人事', '行政', '设计'
    ]
    for term in important_terms:
        if term.lower() in text:
            terms.add(term.lower())
    return terms

JOB_DIRECTION_KEYWORDS = {
    '后端开发': ['后端', 'java', 'python', 'flask', 'django', 'spring', '接口', '数据库', 'mysql', 'sqlite', 'redis'],
    '前端开发': ['前端', 'javascript', 'typescript', 'vue', 'react', 'html', 'css', '小程序'],
    '测试': ['测试', '自动化测试', '接口测试', '性能测试', '缺陷'],
    '嵌入式': ['嵌入式', '单片机', 'stm32', 'gd32', 'mcu', 'pcb', '硬件', 'uart', 'spi', 'i2c'],
    '算法': ['算法', '机器学习', '深度学习', '模型', 'pytorch', 'tensorflow', 'opencv'],
    '财务会计': ['会计', '财务', '出纳', '税务', '审计', '成本', '报表', '金蝶', '用友'],
    '运营': ['运营', '用户增长', '活动策划', '数据分析', '内容运营'],
    '销售': ['销售', '客户', '商务', '渠道', '业绩'],
    '人事行政': ['人事', '招聘', '行政', '薪酬', '绩效']
}

MIN_RECOMMENDATION_SCORE = 35

def detect_job_directions(*values):
    """识别简历或岗位所属方向"""
    text = normalize_match_text(list(values))
    directions = set()
    for direction, keywords in JOB_DIRECTION_KEYWORDS.items():
        if any(keyword.lower() in text for keyword in keywords):
            directions.add(direction)
    return directions

def parse_work_years(value, mode='max'):
    """从工作年限文本中提取年限数字"""
    text = normalize_match_text(value)
    if not text:
        return None
    if any(word in text for word in ['应届', '不限', '无经验']):
        return 0
    numbers = [int(item) for item in re.findall(r'\d+', text)]
    if not numbers:
        return None
    return min(numbers) if mode == 'min' else max(numbers)

def is_education_matched(resume_text, job_education):
    """判断学历要求是否明显满足"""
    required = normalize_match_text(job_education)
    if not required or '不限' in required:
        return True
    education_rank = {'大专': 1, '专科': 1, '本科': 2, '硕士': 3, '研究生': 3, '博士': 4}
    required_rank = max((rank for name, rank in education_rank.items() if name in required), default=0)
    resume_rank = max((rank for name, rank in education_rank.items() if name in resume_text), default=0)
    return required_rank == 0 or resume_rank >= required_rank

def build_job_match_profile(match_text, profile_data):
    """整理候选人画像用于岗位匹配"""
    text = normalize_match_text([match_text, profile_data])
    return {
        'text': text,
        'terms': extract_match_terms(text, profile_data),
        'directions': detect_job_directions(text, profile_data),
        'workYears': parse_work_years(profile_data.get('workYears') if isinstance(profile_data, dict) else text)
    }

def calculate_structured_job_match(match_text, profile_data, job, extra_score=0):
    """按方向、技能、经历和基础条件计算岗位匹配分"""
    profile = build_job_match_profile(match_text, profile_data if isinstance(profile_data, dict) else {})
    job_text = normalize_match_text([
        job.get('title'),
        job.get('jobType'),
        job.get('category'),
        job.get('description'),
        job.get('responsibilities'),
        job.get('requirements'),
        job.get('tags'),
        job.get('industry')
    ])
    job_terms = extract_match_terms(job_text)
    job_directions = detect_job_directions(job_text)
    overlap = sorted(profile['terms'].intersection(job_terms), key=len, reverse=True)

    score = 0
    reasons = []

    if profile['directions'] and job_directions:
        matched_directions = sorted(profile['directions'].intersection(job_directions))
        if matched_directions:
            score += 35
            reasons.append(f"岗位方向匹配：{matched_directions[0]}")
        else:
            score -= 20
            reasons.append('岗位方向不完全一致')
    elif job_directions:
        score += 8

    job_skill_terms = {
        term for term in job_terms
        if any(term in keyword.lower() or keyword.lower() in term for keywords in JOB_DIRECTION_KEYWORDS.values() for keyword in keywords)
    }
    matched_skill_terms = sorted(profile['terms'].intersection(job_skill_terms), key=len, reverse=True)
    if matched_skill_terms:
        ratio = len(matched_skill_terms) / max(len(job_skill_terms), 1)
        score += min(30, 12 + int(ratio * 30))
        reasons.append(f"技能匹配：{'、'.join(matched_skill_terms[:3])}")

    if overlap:
        score += min(20, len(overlap) * 4)
        if len(reasons) < 3:
            reasons.append(f"经历相关：{'、'.join(overlap[:3])}")

    position_text = normalize_match_text([
        profile_data.get('position') if isinstance(profile_data, dict) else '',
        match_text
    ])
    title_terms = extract_match_terms(job.get('title'))
    if title_terms and any(term in position_text for term in title_terms):
        score += 15
        if len(reasons) < 3:
            reasons.append('目标岗位名称接近')

    if is_education_matched(profile['text'], job.get('education')):
        score += 5
        if len(reasons) < 3 and job.get('education'):
            reasons.append('学历要求符合')
    else:
        score -= 15
        reasons.append('学历要求可能不匹配')

    resume_years = profile['workYears']
    job_years = parse_work_years(job.get('experience'), mode='min')
    if job_years is not None:
        if job_years == 0 or (resume_years is not None and resume_years >= job_years):
            score += 5
            if len(reasons) < 3 and job.get('experience'):
                reasons.append('经验要求符合')
        elif resume_years is not None:
            score -= 10

    score += extra_score
    if profile['directions'] and job_directions and not profile['directions'].intersection(job_directions):
        score = min(score, 45)

    clean_reasons = []
    for reason in reasons:
        if reason not in clean_reasons:
            clean_reasons.append(reason)
    return max(0, min(score, 100)), clean_reasons[:5]

def get_resume_match_context(file_id):
    """获取简历推荐岗位所需的文本和身份信息"""
    metadata = load_metadata()
    resume = next((r for r in metadata if r.get('id') == file_id and r.get('status') == 'active'), None)
    if not resume:
        raise ValueError('简历不存在')

    contents = load_resume_contents()
    content_data = contents.get(file_id) or {}
    parsed_info = content_data.get('parsedInfo') or resume.get('parsedInfo') or {}
    if not content_data:
        raise RuntimeError('简历尚未解析')

    match_text = normalize_match_text([
        resume.get('candidateName'),
        resume.get('position'),
        content_data.get('content'),
        parsed_info.get('position'),
        parsed_info.get('skills'),
        parsed_info.get('highlights'),
        parsed_info.get('summary'),
        parsed_info.get('education')
    ])
    return resume, parsed_info, match_text

def calculate_resume_job_match(resume_text, parsed_info, job):
    """计算简历和真实岗位的适配分"""
    return calculate_structured_job_match(resume_text, parsed_info, job)

def load_candidate_favorite_job_ids(candidate_key):
    """读取候选人已加入意向库的职位ID"""
    key = str(candidate_key or '').strip()
    if not key:
        return set()

    with get_job_db_connection() as connection:
        rows = connection.execute(
            '''
            SELECT job_id FROM candidate_favorite_jobs
            WHERE candidate_name = ? OR resume_id = ?
            ''',
            (key, key)
        ).fetchall()

    return {normalize_job_id(row['job_id']) for row in rows if normalize_job_id(row['job_id'])}

def recommend_jobs_for_resume(file_id, limit=6):
    """根据简历解析结果推荐真实岗位"""
    resume, parsed_info, resume_text = get_resume_match_context(file_id)
    candidate_key = resume.get('candidateName') or parsed_info.get('candidateName') or file_id
    favorite_job_ids = load_candidate_favorite_job_ids(candidate_key)
    recommendations = []
    for job in load_real_jobs():
        if not isinstance(job, dict):
            continue
        if normalize_job_id(job.get('id')) in favorite_job_ids:
            continue
        score, reasons = calculate_resume_job_match(resume_text, parsed_info, job)
        if score < MIN_RECOMMENDATION_SCORE:
            continue
        recommendations.append({
            **job,
            'matchScore': score,
            'matchReasons': reasons,
            'recommendedForResumeId': file_id,
            'recommendedForName': resume.get('candidateName') or parsed_info.get('candidateName') or '候选人'
        })

    recommendations.sort(
        key=lambda item: (item.get('matchScore', 0), str(item.get('updatedAt') or '')),
        reverse=True
    )
    return recommendations[:limit]

def get_interview_match_text(report_data, session):
    """整理面试报告推荐岗位所需的匹配文本"""
    target_job = normalize_target_job(session.get('targetedJob'))
    resume_id = session.get('resumeId')
    resume_content = ''
    if resume_id:
        content_data = load_resume_contents().get(resume_id) or {}
        resume_content = content_data.get('content', '')

    messages = session.get('messages') or []
    conversation_text = "\n".join(
        str(message.get('content') or '')
        for message in messages
        if isinstance(message, dict)
    )

    return normalize_match_text([
        session.get('candidateName'),
        session.get('position'),
        session.get('resumePosition'),
        target_job,
        resume_content,
        conversation_text,
        report_data.get('summary'),
        report_data.get('strengths'),
        report_data.get('areasForImprovement'),
        report_data.get('technicalAssessment'),
        report_data.get('projectExperience')
    ])

def calculate_interview_job_match(match_text, report_data, session, job):
    """计算面试报告和真实岗位的适配分"""
    extra_score = 0
    target_job = normalize_target_job(session.get('targetedJob'))
    if target_job and normalize_job_id(target_job.get('id')) == normalize_job_id(job.get('id')):
        extra_score += 30

    report_score = parse_score_value(report_data.get('overallScore'), 75)
    if report_score >= 80:
        extra_score += 10
    elif report_score < 60:
        extra_score -= 10

    profile_data = {
        **(report_data if isinstance(report_data, dict) else {}),
        'position': session.get('position') or session.get('resumePosition'),
        'workYears': session.get('workYears', '')
    }
    return calculate_structured_job_match(match_text, profile_data, job, extra_score=extra_score)

def recommend_jobs_for_interview_report(report_data, session, limit=3):
    """从真实岗位库中为面试报告推荐岗位"""
    match_text = get_interview_match_text(report_data, session)
    candidate_key = session.get('candidateName') or session.get('resumeId')
    favorite_job_ids = load_candidate_favorite_job_ids(candidate_key)
    recommendations = []
    for job in load_real_jobs():
        if not isinstance(job, dict):
            continue
        if normalize_job_id(job.get('id')) in favorite_job_ids:
            continue
        score, reasons = calculate_interview_job_match(match_text, report_data, session, job)
        if score < MIN_RECOMMENDATION_SCORE:
            continue
        recommendations.append({
            **job,
            'matchScore': score,
            'matchReasons': reasons,
            'isRecommended': True
        })

    recommendations.sort(
        key=lambda item: (item.get('matchScore', 0), str(item.get('updatedAt') or '')),
        reverse=True
    )
    return recommendations[:limit]

def normalize_target_job(job):
    """规范化面试目标岗位信息"""
    if not isinstance(job, dict):
        return None

    title = str(job.get('title') or '').strip()
    if not title:
        return None

    return {
        'id': job.get('id'),
        'title': title,
        'company': str(job.get('company') or '').strip(),
        'category': str(job.get('category') or '').strip(),
        'jobType': str(job.get('jobType') or job.get('category') or '').strip(),
        'location': str(job.get('location') or '').strip(),
        'salary': str(job.get('salary') or '').strip(),
        'experience': str(job.get('experience') or '').strip(),
        'education': str(job.get('education') or '').strip(),
        'status': str(job.get('status') or '').strip(),
        'source': str(job.get('source') or '').strip(),
        'sourceUrl': str(job.get('sourceUrl') or '').strip(),
        'description': str(job.get('description') or '').strip(),
        'responsibilities': normalize_text_list(job.get('responsibilities')),
        'requirements': normalize_text_list(job.get('requirements')),
        'tags': normalize_text_list(job.get('tags')),
        'benefits': normalize_text_list(job.get('benefits')),
        'companyInfo': str(job.get('companyInfo') or '').strip(),
        'companySize': str(job.get('companySize') or '').strip(),
        'financingStage': str(job.get('financingStage') or '').strip(),
        'industry': str(job.get('industry') or '').strip(),
        'contactName': str(job.get('contactName') or '').strip(),
        'contactRole': str(job.get('contactRole') or '').strip(),
        'workAddress': str(job.get('workAddress') or '').strip()
    }

def get_interview_position(resume_position, target_job=None):
    """确定本次面试真正使用的岗位名称"""
    if target_job and target_job.get('title'):
        return target_job['title']
    return resume_position or '未知岗位'

def format_target_job_context(target_job):
    """把目标岗位整理成可放入提示词的文本"""
    if not target_job:
        return "未选择真实岗位，本次按简历中的求职意向进行通用面试。"

    responsibilities = "\n".join([f"{idx}. {item}" for idx, item in enumerate(target_job.get('responsibilities', []), 1)]) or "暂无岗位职责"
    requirements = "\n".join([f"{idx}. {item}" for idx, item in enumerate(target_job.get('requirements', []), 1)]) or "暂无任职要求"
    tags = "、".join(target_job.get('tags', [])) or "暂无标签"

    return f"""本次真实目标岗位
岗位名称：{target_job.get('title') or '未知'}
公司：{target_job.get('company') or '未知'}
城市：{target_job.get('location') or '未知'}
薪资：{target_job.get('salary') or '未知'}
经验要求：{target_job.get('experience') or '未知'}
学历要求：{target_job.get('education') or '未知'}
岗位方向：{target_job.get('jobType') or target_job.get('category') or '未知'}
技能标签：{tags}
岗位摘要：{target_job.get('description') or '暂无'}

岗位职责
{responsibilities}

任职要求
{requirements}

公司信息
公司：{target_job.get('company') or '未知'}
规模：{target_job.get('companySize') or '未知'}
融资阶段：{target_job.get('financingStage') or '未知'}
行业：{target_job.get('industry') or '未知'}
地址：{target_job.get('workAddress') or '未知'}
"""

def generate_recommended_jobs(report_data, session):
    """根据面试表现从真实岗位库中推荐职位"""
    return recommend_jobs_for_interview_report(report_data, session)


def get_interview_system_prompt(candidate_name, position, resume_content=None, target_job=None, resume_position=None):
    """生成专业面试官系统提示 - 涵盖7大面试维度"""
    
    # 识别岗位类型
    position_type = detect_position_type(position)
    focus = get_position_focus_areas(position_type)
    target_job_context = format_target_job_context(target_job)
    
    # 构建技术基础问题列表
    tech_basics_str = '\n'.join([f'   - {item}' for item in focus['technical_basics']])
    system_design_str = '\n'.join([f'   - {item}' for item in focus['system_design']])
    coding_str = '\n'.join([f'   - {item}' for item in focus['coding']])
    engineering_str = '\n'.join([f'   - {item}' for item in focus['engineering']])
    
    base_prompt = f"""你是一位来自顶尖互联网公司的资深技术面试官，正在面试候选人「{candidate_name}」，本次面试岗位是「{position}」（{focus['name']}方向）。

请把「本次面试岗位」作为唯一面试目标。如果候选人简历中的求职意向是「{resume_position or position}」，但与本次真实目标岗位不一致，你需要以真实目标岗位为准，不要沿用简历原岗位方向。

面试开场必须明确告诉候选人：本次面试岗位是「{position}」。如果有公司名称，也要一起说明。

{target_job_context}

面试目标
1. 全面评估候选人能力：技术深度、工程素养、解决问题能力、沟通表达
2. 挖掘隐藏优势：发现简历未充分展示的技能和经验
3. 模拟真实面试：让候选人体验真实的互联网公司技术面试流程
4. 围绕目标岗位追问：问题要优先覆盖真实岗位的岗位职责、任职要求和技能标签

面试七大维度（请根据面试进展灵活覆盖）

维度一：技术基础
考察候选人的计算机基础知识，重点关注：
{tech_basics_str}

示例问题：
- 请解释一下TCP三次握手和四次挥手的过程？为什么是三次和四次？
- HashMap的底层实现原理是什么？如何解决哈希冲突？
- 进程和线程的区别是什么？什么是协程？

维度二：系统设计
考察候选人的架构设计能力：
{system_design_str}

示例问题：
- 如何设计一个支持百万QPS的短链系统？
- 如果让你设计一个分布式限流方案，你会怎么做？
- 缓存与数据库的一致性问题如何解决？

维度三：编码能力
考察代码实现和问题解决思路：
{coding_str}

面试技巧：不要求立即写代码，但要询问解题思路、时间/空间复杂度分析

维度四：项目经验（STAR法则）
深入挖掘候选人的项目经历：
1. Situation：项目背景是什么？
2. Task：你承担什么角色和职责？
3. Action：你具体做了什么？技术方案如何？
4. Result：最终成果如何？有哪些量化指标？

追问方向：
- 项目中最大的技术挑战是什么？如何解决的？
- 如果重来一次，你会如何改进这个项目？
- 这个项目给你带来了哪些技术成长？

维度五：工程实践
考察候选人的工程素养：
{engineering_str}

示例问题：
- 你们团队是如何保证代码质量的？
- 上线流程是怎样的？如何做到快速回滚？
- 如何定位和解决线上问题？

维度六：产品思维与业务理解
- 你负责的功能是如何确定需求的？
- 如果发现某个功能的用户使用率很低，你会如何分析？
- 如何权衡技术债务和业务迭代速度？

维度七：文化匹配与职业规划
- 你为什么对这个岗位感兴趣？
- 未来3-5年的职业规划是什么？
- 你是如何保持技术学习和成长的？

面试流程指引

第一阶段（热身）：
- 简短自我介绍，建立融洽氛围
- 请候选人做2-3分钟的自我介绍
- 开场时说明本次面试岗位，并提醒候选人回答时尽量围绕该岗位要求展开

第二阶段（项目深挖）：
- 从简历中选择1-2个核心项目深入追问
- 使用STAR法则挖掘细节
- 追问项目经历与目标岗位职责、任职要求之间的匹配度

第三阶段（技术考察）：
- 根据岗位类型提出技术基础问题
- 可适当追问一道系统设计或编码思路题
- 如果真实岗位中写明具体技术关键词，优先围绕这些关键词发问

第四阶段（工程素养）：
- 了解候选人的工程实践经验
- 考察代码质量意识和团队协作能力

第五阶段（收尾）：
- 了解职业规划和动机
- 询问候选人是否有问题想问

面试风格要求
1. 专业严谨：问题要有深度，模拟真实大厂面试风格
2. 循序渐进：先问基础，再追问细节
3. 每次只问一个问题：给候选人充分思考和回答的空间
4. 善于追问：根据候选人回答继续深入挖掘
5. 鼓励为主：对好的回答给予肯定，引导候选人展示更多
6. 记录关键信息：留意候选人提到的所有技能、项目、成就，用于后续简历优化
7. 目标岗位优先：所有问题都应服务于判断候选人是否适合「{position}」，不要偏向简历原岗位

输出格式要求
1. 只输出普通中文文本，不要输出 Markdown 格式。
2. 禁止出现井号标题符号、星号加粗符号、反引号、代码块符号。
3. 不要使用 ###、**、`、``` 这类格式符号。
4. 技术名词直接用普通文本写出，例如 cache-misses、branch-misses、LLVM Pass。
5. 如需分点，只使用中文短句或数字编号。

请用中文进行面试。"""

    if resume_content and resume_content not in ["无法提取简历内容", ""] and not resume_content.startswith("提取失败"):
        base_prompt += f"""

候选人简历内容

{resume_content}

基于简历的面试策略

请仔细阅读以上简历，在面试时：

1. 验证简历信息：
   - 确认项目经历的真实性和深度
   - 核实技术栈的熟练程度

2. 项目深挖方向：
   - 简历中最亮眼的项目是什么？重点追问
   - 项目描述中有哪些模糊的地方需要澄清？

3. 发现隐藏技能：
   - 简历中提到但未详细说明的技术
   - 可能参与过但未列出的其他项目
   - 团队管理、跨部门协作等软性能力

4. 量化成果挖掘：
   - 询问具体的性能提升数据
   - 了解业务增长或成本节省的量化指标
   - 挖掘获得的荣誉、证书、奖项等"""
    
    return base_prompt

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """上传简历文件"""
    if 'file' not in request.files:
        return jsonify({'error': '没有选择文件'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': '只支持PDF格式文件'}), 400
    
    # 生成唯一文件名
    original_filename = secure_filename(file.filename)
    file_id = str(uuid.uuid4())
    filename = f"{file_id}_{original_filename}"
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # 保存文件
    file.save(file_path)
    
    # 检查文件大小
    file_size_mb = get_file_size_mb(file_path)
    
    # 获取额外信息
    candidate_name = request.form.get('candidateName', '未知')
    position = request.form.get('position', '未知')
    
    # 保存元数据
    metadata = load_metadata()
    resume_data = {
        'id': file_id,
        'originalName': original_filename,
        'filename': filename,
        'candidateName': candidate_name,
        'position': position,
        'size': round(file_size_mb, 2),
        'uploadTime': datetime.now().isoformat(),
        'status': 'active',
        'contentExtracted': False  # 标记是否已提取内容
    }
    metadata.append(resume_data)
    save_metadata(metadata)

    # 上传后立即执行解析，确保手动编辑时可以看到预填内容
    parse_result = None
    parse_error = ''
    try:
        parse_result = extract_and_cache_resume_data(file_id)
    except Exception as e:
        parse_error = str(e)
        print(f"上传后自动解析失败: {e}")
    
    return jsonify({
        'message': '上传成功',
        'data': resume_data,
        'autoParsed': bool(parse_result),
        'autoStructured': bool(parse_result and parse_result.get('structuredSaved')),
        'parseMessage': '' if parse_result else f'自动解析失败: {parse_error}'
    }), 200

@app.route('/api/resumes', methods=['GET'])
def get_resumes():
    """获取所有简历列表"""
    metadata = load_metadata()
    active_resumes = []
    
    # 过滤并规范化数据，避免历史脏数据导致接口报错
    for item in metadata:
        if not isinstance(item, dict):
            continue
        if item.get('status', 'active') != 'active':
            continue
        
        normalized = {
            **item,
            'id': str(item.get('id') or ''),
            'originalName': str(item.get('originalName') or item.get('filename') or '未知文件'),
            'candidateName': str(item.get('candidateName') or '未知'),
            'position': str(item.get('position') or '未知'),
            'uploadTime': str(item.get('uploadTime') or ''),
            'filename': str(item.get('filename') or '')
        }
        active_resumes.append(normalized)
    
    # 按上传时间倒序排列（缺失时间的记录排到最后）
    active_resumes.sort(key=lambda x: x.get('uploadTime', ''), reverse=True)
    return jsonify({'data': active_resumes}), 200

@app.route('/api/resumes/<file_id>', methods=['GET'])
def get_resume(file_id):
    """获取单个简历详情"""
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id), None)
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    return jsonify({'data': resume}), 200

@app.route('/api/resumes/<file_id>/download', methods=['GET'])
def download_resume(file_id):
    """下载简历文件"""
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id), None)
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 根据是否为优化简历选择不同的目录
    if resume.get('isOptimized'):
        file_path = os.path.join(GENERATED_RESUMES_FOLDER, resume['filename'])
    else:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], resume['filename'])
    
    if not os.path.exists(file_path):
        return jsonify({'error': '文件不存在'}), 404
    
    return send_file(file_path, as_attachment=True, download_name=resume['originalName'])

@app.route('/api/resumes/<file_id>/preview', methods=['GET'])
def preview_resume(file_id):
    """预览简历文件"""
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id), None)
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 根据是否为优化简历选择不同的目录
    if resume.get('isOptimized'):
        file_path = os.path.join(GENERATED_RESUMES_FOLDER, resume['filename'])
    else:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], resume['filename'])
    
    if not os.path.exists(file_path):
        return jsonify({'error': '文件不存在'}), 404
    
    return send_file(file_path, mimetype='application/pdf')

@app.route('/api/resumes/<file_id>/extract', methods=['POST'])
def extract_resume_content(file_id):
    """提取简历内容（OCR识别 + AI智能解析）"""
    try:
        result = extract_and_cache_resume_data(file_id)
    except ValueError:
        return jsonify({'error': '简历不存在'}), 404
    except FileNotFoundError:
        return jsonify({'error': '文件不存在'}), 404
    except Exception as e:
        return jsonify({'error': f'提取失败: {str(e)}'}), 500

    return jsonify({
        'content': result['content'],
        'method': result['method'],
        'parsedInfo': result['parsedInfo'],
        'structuredSaved': result.get('structuredSaved', False),
        'recommendedJobs': recommend_jobs_for_resume(file_id),
        'message': '内容提取并解析成功' if result.get('parsedInfo') else ('内容提取成功' if result.get('method') != 'error' else '提取失败')
    }), 200

@app.route('/api/resumes/<file_id>/recommended-jobs', methods=['GET'])
def get_resume_recommended_jobs(file_id):
    """根据已解析简历推荐真实岗位"""
    try:
        jobs = recommend_jobs_for_resume(file_id)
    except ValueError:
        return jsonify({'error': '简历不存在'}), 404
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'推荐失败: {str(e)}'}), 500

    return jsonify({'data': jobs}), 200

@app.route('/api/resumes/<file_id>/content', methods=['GET'])
def get_resume_content(file_id):
    """获取已提取的简历内容"""
    contents = load_resume_contents()
    content_data = contents.get(file_id)
    
    if not content_data:
        return jsonify({'error': '内容尚未提取'}), 404
    
    return jsonify({'data': content_data}), 200

@app.route('/api/resumes/<file_id>', methods=['DELETE'])
def delete_resume(file_id):
    """删除简历"""
    metadata = load_metadata()
    resume_index = next((i for i, r in enumerate(metadata) if r['id'] == file_id), None)
    
    if resume_index is None:
        return jsonify({'error': '简历不存在'}), 404
    
    # 标记为已删除
    metadata[resume_index]['status'] = 'deleted'
    save_metadata(metadata)
    
    # 删除缓存的内容
    contents = load_resume_contents()
    if file_id in contents:
        del contents[file_id]
        save_resume_contents(contents)

    # 删除缓存的结构化数据
    structured_data = load_structured_data()
    if file_id in structured_data:
        del structured_data[file_id]
        save_structured_data(structured_data)
    
    # 可选：删除物理文件
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], metadata[resume_index]['filename'])
    if os.path.exists(file_path):
        os.remove(file_path)
    
    return jsonify({'message': '删除成功'}), 200

# ==================== AI 面试功能 ====================

@app.route('/api/interview/start', methods=['POST'])
def start_interview():
    """开始面试会话"""
    data = request.get_json()
    resume_id = data.get('resumeId')
    target_job = normalize_target_job(data.get('targetedJob'))
    
    if not resume_id:
        return jsonify({'error': '请选择简历'}), 400
    
    # 获取简历信息
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == resume_id and r.get('status') == 'active'), None)
    
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 获取简历内容（如果已提取）
    contents = load_resume_contents()
    content_data = contents.get(resume_id)
    resume_content = content_data.get('content') if content_data else None
    
    resume_position = resume.get('position', '未知')
    interview_position = get_interview_position(resume_position, target_job)

    # 创建面试会话
    session_id = str(uuid.uuid4())
    system_prompt = get_interview_system_prompt(
        resume['candidateName'], 
        interview_position,
        resume_content,
        target_job,
        resume_position
    )
    
    # 获取AI的开场白
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请开始面试。先明确告诉候选人本次面试岗位是「{interview_position}」，再结合简历和目标岗位要求提出第一个问题。"}
            ],
            stream=False
        )
        
        ai_message = response.choices[0].message.content
        
        # 保存会话
        sessions = load_interview_sessions()
        sessions[session_id] = {
            'resumeId': resume_id,
            'candidateName': resume['candidateName'],
            'position': interview_position,
            'resumePosition': resume_position,
            'targetedJob': target_job,
            'systemPrompt': system_prompt,
            'hasResumeContent': resume_content is not None,
            'messages': [
                {"role": "assistant", "content": ai_message}
            ],
            'startTime': datetime.now().isoformat(),
            'status': 'active'
        }
        save_interview_sessions(sessions)
        
        return jsonify({
            'sessionId': session_id,
            'message': ai_message,
            'candidateName': resume['candidateName'],
            'position': interview_position,
            'resumePosition': resume_position,
            'targetedJob': target_job,
            'hasResumeContent': resume_content is not None
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'AI服务错误: {str(e)}'}), 500

@app.route('/api/interview/chat', methods=['POST'])
def interview_chat():
    """面试对话"""
    data = request.get_json()
    session_id = data.get('sessionId')
    user_message = data.get('message')
    
    if not session_id or not user_message:
        return jsonify({'error': '参数不完整'}), 400
    
    # 获取会话
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    if session.get('status') != 'active':
        return jsonify({'error': '会话已结束'}), 400
    
    # 构建消息历史
    messages = [{"role": "system", "content": session['systemPrompt']}]
    
    # 添加历史消息
    for msg in session['messages']:
        messages.append({"role": msg['role'], "content": msg['content']})
    
    # 添加用户新消息
    messages.append({"role": "user", "content": user_message})
    
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            stream=False
        )
        
        ai_message = response.choices[0].message.content
        
        # 更新会话
        session['messages'].append({"role": "user", "content": user_message})
        session['messages'].append({"role": "assistant", "content": ai_message})
        sessions[session_id] = session
        save_interview_sessions(sessions)
        
        return jsonify({
            'message': ai_message
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'AI服务错误: {str(e)}'}), 500

@app.route('/api/interview/chat/stream', methods=['POST'])
def interview_chat_stream():
    """面试对话（流式响应）"""
    data = request.get_json()
    session_id = data.get('sessionId')
    user_message = data.get('message')
    
    if not session_id or not user_message:
        return jsonify({'error': '参数不完整'}), 400
    
    # 获取会话
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    if session.get('status') != 'active':
        return jsonify({'error': '会话已结束'}), 400
    
    # 构建消息历史
    messages = [{"role": "system", "content": session['systemPrompt']}]
    
    for msg in session['messages']:
        messages.append({"role": msg['role'], "content": msg['content']})
    
    messages.append({"role": "user", "content": user_message})
    
    def generate():
        full_response = ""
        try:
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                stream=True
            )
            
            for chunk in response:
                if chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield f"data: {json.dumps({'content': content})}\n\n"
            
            # 流结束后保存会话
            session['messages'].append({"role": "user", "content": user_message})
            session['messages'].append({"role": "assistant", "content": full_response})
            sessions[session_id] = session
            save_interview_sessions(sessions)
            
            yield f"data: {json.dumps({'done': True})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/interview/end', methods=['POST'])
def end_interview():
    """结束面试会话"""
    data = request.get_json()
    session_id = data.get('sessionId')
    
    if not session_id:
        return jsonify({'error': '会话ID不能为空'}), 400
    
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    # 更新会话状态
    session['status'] = 'ended'
    session['endTime'] = datetime.now().isoformat()
    sessions[session_id] = session
    save_interview_sessions(sessions)
    
    return jsonify({'message': '面试已结束'}), 200

@app.route('/api/interview/history', methods=['GET'])
def get_interview_history():
    """获取面试历史"""
    sessions = load_interview_sessions()
    history = []
    
    for session_id, session in sessions.items():
        history.append({
            'sessionId': session_id,
            'candidateName': session.get('candidateName'),
            'position': session.get('position'),
            'startTime': session.get('startTime'),
            'endTime': session.get('endTime'),
            'status': session.get('status'),
            'messageCount': len(session.get('messages', []))
        })
    
    # 按开始时间倒序排列
    history.sort(key=lambda x: x['startTime'], reverse=True)
    
    return jsonify({'data': history}), 200

@app.route('/api/interview/<session_id>', methods=['GET'])
def get_interview_session(session_id):
    """获取面试会话详情"""
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    return jsonify({'data': session}), 200

# ==================== 简历生成功能 ====================

@app.route('/api/interview/<session_id>/analyze', methods=['POST'])
def analyze_interview(session_id):
    """分析面试对话，提取隐藏技能和优势"""
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    if session.get('status') != 'ended':
        return jsonify({'error': '请先结束面试'}), 400
    
    # 获取原始简历内容
    resume_id = session.get('resumeId')
    contents = load_resume_contents()
    original_resume_content = contents.get(resume_id, {}).get('content', '')
    target_job = normalize_target_job(session.get('targetedJob'))
    target_job_context = format_target_job_context(target_job)
    
    # 构建分析提示
    messages_text = "\n".join([
        f"{'面试官' if msg['role'] == 'assistant' else '候选人'}: {msg['content']}"
        for msg in session.get('messages', [])
    ])
    
    analysis_prompt = f"""你是一位资深的简历优化专家和技术面试评估师。请深入分析以下面试对话，从多个维度提取候选人展现但原简历中未充分体现的信息。

## 原始简历内容
{original_resume_content[:3000] if original_resume_content else '无原始简历内容'}

## 本次面试目标岗位
{target_job_context}

## 面试对话记录
{messages_text}

---

## 请从以下维度进行深度分析：

### 1. 技术能力分析
- 面试中提到的技术栈（包括简历未列出的）
- 候选人与目标岗位职责、任职要求的匹配度
- 技术深度体现（原理理解、源码阅读、底层知识）
- 系统设计能力（架构思维、性能优化经验）
- 编码能力（算法能力、代码规范意识）

### 2. 项目经验深挖
- 候选人在面试中详细描述但简历中只是简单提及的项目
- 项目中的技术亮点和创新点
- 量化的业务成果（性能提升X%、成本降低X%等）
- 遇到的挑战和解决方案

### 3. 软技能评估
- 沟通表达能力
- 逻辑思维能力
- 学习能力和成长轨迹
- 团队协作和领导力

### 4. 职业素养
- 工程实践意识（代码质量、测试、监控等）
- 产品思维和业务理解
- 职业规划清晰度

### 5. 岗位匹配分析
- 如果原简历求职意向与目标岗位不一致，指出需要改写的位置
- 提取可以服务于目标岗位的项目、技能和课程
- 标记目标岗位要求中候选人已经体现和暂未体现的能力

### 6. 隐藏亮点
- 证书、获奖、开源贡献等
- 跨领域技能或特殊经历
- 独特的个人优势

---

请严格按照以下JSON格式返回分析结果（不要添加任何其他内容）：
{{
    "technicalSkills": {{
        "newSkills": ["面试中发现的新技能1", "技能2"],
        "skillDepth": [
            {{"skill": "技能名称", "depth": "深入程度描述", "evidence": "面试中的证据"}},
        ],
        "systemDesign": "系统设计能力评估描述",
        "codingAbility": "编码能力评估描述"
    }},
    "projectHighlights": [
        {{
            "projectName": "项目名称",
            "period": "时间段",
            "position": "担任角色",
            "description": "项目描述（包含面试中挖掘的细节）",
            "technicalPoints": ["技术亮点1", "技术亮点2"],
            "quantifiedResults": ["量化成果1", "量化成果2"],
            "challenges": "遇到的挑战及解决方案",
            "isNewDiscovery": true
        }}
    ],
    "softSkills": {{
        "communication": {{"rating": "优秀/良好/一般", "evidence": "具体表现"}},
        "problemSolving": {{"rating": "优秀/良好/一般", "evidence": "具体表现"}},
        "learning": {{"rating": "优秀/良好/一般", "evidence": "具体表现"}},
        "teamwork": {{"rating": "优秀/良好/一般", "evidence": "具体表现"}}
    }},
    "engineeringPractice": {{
        "codeQuality": "代码质量意识描述",
        "testing": "测试经验描述",
        "devops": "DevOps/工程化经验描述"
    }},
    "hiddenGems": {{
        "certificates": ["证书/荣誉1", "证书2"],
        "awards": ["获奖经历"],
        "openSource": ["开源贡献"],
        "uniqueStrengths": ["独特优势1", "独特优势2"]
    }},
    "resumeImprovements": [
        {{"section": "改进位置", "current": "当前描述", "suggested": "建议改进为", "reason": "改进原因"}}
    ],
    "targetJobAlignment": {{
        "targetPosition": "{target_job.get('title') if target_job else session.get('position', '')}",
        "resumePositionConflict": "是否存在求职意向不一致",
        "matchedRequirements": ["已匹配的目标岗位要求"],
        "missingRequirements": ["暂未体现的目标岗位要求"],
        "rewriteFocus": ["优化简历时应重点突出的方向"]
    }},
    "overallSummary": "整体评估总结，200字左右",
    "interviewInsights": "面试过程中发现的关键信息，这些信息应该在简历中突出展示"
}}"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位专业的简历优化专家，擅长从面试对话中提取有价值的信息。请严格按照JSON格式返回结果。"},
                {"role": "user", "content": analysis_prompt}
            ],
            stream=False
        )
        
        analysis_text = response.choices[0].message.content
        
        # 尝试解析JSON
        json_match = re.search(r'\{[\s\S]*\}', analysis_text)
        if json_match:
            try:
                analysis_data = json.loads(json_match.group())
            except json.JSONDecodeError:
                analysis_data = {
                    "raw": analysis_text,
                    "newSkills": [],
                    "newStrengths": [],
                    "newProjects": [],
                    "newCertificates": [],
                    "improvements": [],
                    "summary": "分析完成，但无法解析结构化数据"
                }
        else:
            analysis_data = {
                "raw": analysis_text,
                "newSkills": [],
                "newStrengths": [],
                "newProjects": [],
                "newCertificates": [],
                "improvements": [],
                "summary": analysis_text[:500]
            }
        
        return jsonify({
            'analysis': analysis_data,
            'sessionId': session_id
        }), 200
        
    except Exception as e:
        import traceback
        print(f"分析错误: {traceback.format_exc()}")
        return jsonify({'error': f'分析失败: {str(e)}'}), 500

@app.route('/api/interview/<session_id>/generate-resume', methods=['POST'])
def generate_optimized_resume(session_id):
    """根据面试结果生成优化后的简历PDF"""
    data = request.get_json()
    analysis_data = data.get('analysis', {})
    
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    # 获取原始简历信息
    resume_id = session.get('resumeId')
    metadata = load_metadata()
    original_resume = next((r for r in metadata if r['id'] == resume_id), None)
    
    if not original_resume:
        return jsonify({'error': '原始简历不存在'}), 404
    
    # 获取原始简历内容
    contents = load_resume_contents()
    original_content = contents.get(resume_id, {}).get('content', '')
    original_structured_data = get_structured_resume_for_optimization(resume_id, original_resume)
    if not is_valid_resume_content(original_content) and original_structured_data:
        original_content = structured_resume_to_text(original_structured_data)
    
    # 获取面试对话记录（用于提取更多信息）
    messages_text = "\n".join([
        f"{'面试官' if msg['role'] == 'assistant' else '候选人'}: {msg['content']}"
        for msg in session.get('messages', [])
    ])
    target_job = normalize_target_job(session.get('targetedJob'))
    target_position = get_interview_position(original_resume.get('position', ''), target_job)
    target_city = target_job.get('location', '') if target_job else ''
    target_salary = target_job.get('salary', '') if target_job else ''
    target_job_context = format_target_job_context(target_job)
    
    # 如果没有传入analysis数据，自动进行分析
    if not analysis_data or not analysis_data.get('newSkills'):
        try:
            analysis_prompt = f"""你是一位专业的简历优化专家。请分析以下面试对话，找出候选人在面试中展现但原简历中未体现的技能、优势、项目经验等，并判断这些内容如何服务于本次目标岗位。

【原始简历内容】
{original_content[:3000] if original_content else '无原始简历内容'}

【本次目标岗位】
{target_job_context}

【面试对话记录】
{messages_text}

请分析并提取以下JSON格式（不要添加任何其他内容）：
{{
    "newSkills": ["技能1", "技能2"],
    "newStrengths": ["优势1", "优势2"],
    "newProjects": [],
    "newCertificates": [],
    "improvements": []
}}"""
            
            analysis_response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一位专业的简历优化专家。请严格按照JSON格式返回结果。"},
                    {"role": "user", "content": analysis_prompt}
                ],
                stream=False
            )
            
            analysis_text = analysis_response.choices[0].message.content
            json_match = re.search(r'\{[\s\S]*\}', analysis_text)
            if json_match:
                analysis_data = json.loads(json_match.group())
        except Exception as e:
            print(f"自动分析错误: {e}")
            analysis_data = {}
    
    # 提取新格式的分析数据（兼容旧格式）
    tech_skills = analysis_data.get('technicalSkills', {})
    new_skills = tech_skills.get('newSkills', analysis_data.get('newSkills', []))
    project_highlights = analysis_data.get('projectHighlights', analysis_data.get('newProjects', []))
    hidden_gems = analysis_data.get('hiddenGems', {})
    soft_skills = analysis_data.get('softSkills', {})
    engineering = analysis_data.get('engineeringPractice', {})
    interview_insights = analysis_data.get('interviewInsights', analysis_data.get('summary', ''))
    allowed_new_project_names = collect_project_names_from_analysis(project_highlights)
    allowed_new_skill_names = {str(skill).strip() for skill in new_skills if str(skill).strip()}
    
    # 使用AI生成完整的简历数据结构
    original_structured_json = json.dumps(original_structured_data or {}, ensure_ascii=False, indent=2)

    resume_generation_prompt = f"""你是一位顶尖互联网公司的HR和简历优化专家。请根据以下信息生成一份**显著优化**的专业简历。

## 核心优化原则
1. **突出面试中发现的新信息**：这些是原简历遗漏但实际具备的能力
2. **量化一切可量化的成果**：用数据说话（提升X%、节省Y万、服务Z用户）
3. **STAR法则描述经历**：情境-任务-行动-结果，让每段经历有说服力
4. **技术深度外显**：不只列技术栈，要体现原理理解和实战经验
5. **差异化竞争力**：突出独特优势，区别于普通候选人
6. **目标岗位对齐**：如果原简历求职意向与本次目标岗位不一致，必须改成目标岗位，不得继续保留原岗位方向
7. **事实信息绝对不能编造**：姓名、电话、邮箱、学校、专业、学历、证书、荣誉、项目名称、项目时间、工作经历公司和任职时间必须来自原简历或面试者明确说过的信息
8. **没有工作经验就保持为空**：如果原简历没有工作经验，不允许为了贴合岗位生成虚假公司、虚假任职经历或虚假年限

---

## 原始简历内容
{original_content[:4000] if original_content else '无原始简历内容'}

---

## 原始简历结构化事实（必须以此为准）
{original_structured_json[:6000] if original_structured_json else '{}'}

---

## 本次目标岗位（优化简历必须以此为准）
{target_job_context}

简历求职意向必须写为：
- 职位：{target_position}
- 城市：{target_city or '按目标岗位城市填写'}
- 期望薪资：{target_salary or '按目标岗位薪资范围填写'}

如果原简历的求职意向不是「{target_position}」，请直接改为「{target_position}」。项目、技能、课程、自我评价都要围绕该岗位职责和任职要求重写。
除求职意向中的目标职位、城市、薪资外，不得修改原简历中的事实信息。

	---

## 面试对话摘要（重要！包含大量简历遗漏的信息）
{messages_text[:4000] if messages_text else '无面试对话'}

---

## 面试中发现的关键新信息（必须整合进简历）

### 新发现的技能
{json.dumps(new_skills, ensure_ascii=False, indent=2)}

### 项目亮点和量化成果
{json.dumps(project_highlights, ensure_ascii=False, indent=2)}

### 隐藏优势（证书、荣誉、开源等）
{json.dumps(hidden_gems, ensure_ascii=False, indent=2)}

### 软技能表现
{json.dumps(soft_skills, ensure_ascii=False, indent=2)}

### 工程实践能力
{json.dumps(engineering, ensure_ascii=False, indent=2)}

### 面试洞察总结
{interview_insights}

---

## 候选人基本信息
- 姓名: {original_resume.get('candidateName', '')}
- 原简历职位: {original_resume.get('position', '')}
- 本次目标职位: {target_position}

---

## 请生成优化后的简历JSON

严格按照以下格式返回（不要添加任何其他内容）：
{{
    "motto": "个人格言（体现专业追求，如：追求极致的代码质量，用技术创造业务价值）",
    "personalSummary": "个人优势摘要（120-180字）：用正式应聘简历语言概括候选人与目标岗位相关的核心能力、项目经验和可带来的价值，不能写岗位关键词列表，也不要写匹配岗位要求清单",
    "basicInfo": {{
        "姓名": "候选人姓名",
        "性别": "男/女",
        "年龄": "XX岁",
        "籍贯": "省份城市",
        "工作年限": "X年经验/应届生",
        "电话": "手机号码",
        "邮箱": "邮箱地址"
    }},
    "jobIntention": {{
        "职位": "{target_position}",
        "城市": "{target_city or '目标岗位城市'}",
        "期望薪资": "{target_salary or '目标岗位薪资范围'}",
        "到岗": "到岗时间"
    }},
    "education": {{
        "时间": "YYYY-MM ~ YYYY-MM",
        "学校": "学校名称",
        "专业": "专业名称（学历）",
        "专业成绩": "GPA或排名（如有）",
        "主修课程": "与目标职位相关的核心课程"
    }},
    "workExperience": [
        {{
            "period": "YYYY-MM ~ YYYY-MM",
            "company": "公司名称",
            "position": "职位名称",
            "responsibilities": [
                "【核心职责】使用STAR法则描述：在什么背景下，承担什么任务，采取了什么行动，取得了什么量化成果",
                "【技术贡献】具体的技术方案、架构设计、性能优化等，体现技术深度",
                "【业务价值】量化的业务成果，如提升X%转化率、降低Y%延迟、支撑Z万日活"
            ]
        }}
    ],
    "projects": [
        {{
            "projectName": "项目名称",
            "period": "项目时间",
            "position": "担任角色（如：核心开发/技术负责人）",
            "description": "项目背景、技术栈、核心价值",
            "responsibilities": [
                "【技术亮点】具体的技术方案和实现细节，体现技术深度",
                "【个人贡献】明确个人承担的工作和创新点",
                "【成果量化】性能提升数据、用户影响、成本节省等"
            ],
            "highlights": "项目中最值得一提的技术亮点或创新点（来自面试）"
        }}
    ],
    "skills": {{
        "核心技能": {{
            "description": "最擅长的技术领域，体现深度（如：精通Redis，熟悉源码，有大规模集群运维经验）",
            "level": 90
        }},
        "技术栈": {{
            "description": "完整的技术栈列表，按类别组织",
            "level": 85
        }},
        "工程能力": {{
            "description": "工程化能力，如CI/CD、代码质量、性能优化等",
            "level": 80
        }},
        "软技能": {{
            "description": "沟通协作、问题解决、学习能力等软技能",
            "level": 75
        }}
    }},
    "certificates": [
        "证书/荣誉（附获得时间，如：AWS认证解决方案架构师-2023）"
    ],
    "selfEvaluation": "自我评价（150-200字）：结合面试中展现的实际优势，突出技术追求、学习能力、解决问题的方法论，避免空洞的形容词，用具体事例支撑"
}}

---

## 优化要点提醒

1. **工作经验优化**：
   - 每条职责都要有量化数据
   - 突出面试中提到但简历遗漏的成果
   - 使用强动词开头：主导、设计、优化、构建

2. **项目经验优化**：
   - 整合面试中挖掘的技术细节
   - 添加面试中提到的技术挑战和解决方案
   - 突出个人贡献而非团队成果

3. **技能优化**：
   - 添加面试中发现的新技能
   - 技能描述体现深度，不只是罗列
   - level评分基于面试表现调整

4. **自我评价**：
   - 融入面试中展现的软技能证据
   - 避免"热爱学习""积极主动"等空话
   - 用1-2个具体事例支撑
   - 与个人优势摘要区分开，自我评价更偏个人风格和职业素养

5. **必须整合的新信息**：
   - 面试中提到的所有新技能必须出现
   - 面试中明确详述的新项目可以追加到项目经历
   - 原有项目经历必须保留，不能删除或替换
   - 原有技能必须保留，新技能只能追加
   - 证书、荣誉、开源贡献必须添加

6. **目标岗位强约束**：
   - `jobIntention.职位` 必须是「{target_position}」
   - `jobIntention.城市` 优先使用「{target_city or '目标岗位城市'}」
   - `jobIntention.期望薪资` 优先参考「{target_salary or '目标岗位薪资'}」
   - 技能、项目、个人优势摘要和自我评价必须自然围绕目标岗位要求，不要继续写成原简历岗位
   - 不要生成“岗位关键词”或“匹配岗位要求”模块

7. **事实保真强约束**：
   - 不得把模板示例当成真实内容
   - 不得新增原简历没有的学校、公司、证书、荣誉和工作经历
   - 原简历已有证书荣誉必须完整保留
   - 原简历没有工作经历时，`workExperience` 必须返回空数组
   - 原简历项目可以优化措辞，但项目名称、时间和担任角色必须保持原样
   - 新项目只有在面试对话或面试分析中明确出现时才能追加"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位资深的简历优化专家。请根据提供的信息，生成一份专业、有竞争力的简历数据。严格按照JSON格式返回，不添加任何解释。"},
                {"role": "user", "content": resume_generation_prompt}
            ],
            stream=False
        )
        
        resume_text = response.choices[0].message.content
        
        # 解析JSON
        json_match = re.search(r'\{[\s\S]*\}', resume_text)
        if not json_match:
            return jsonify({'error': '无法从AI响应中提取简历数据'}), 500
        
        try:
            resume_data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            print(f"JSON解析错误: {e}")
            print(f"原始响应: {resume_text[:1000]}")
            return jsonify({'error': f'简历数据格式错误: {str(e)}'}), 500
        
        # 验证必要字段
        if not resume_data.get('basicInfo'):
            return jsonify({'error': '简历数据不完整，缺少基本信息'}), 500

        # 强制把优化简历的求职意向对齐本次目标岗位
        resume_data = apply_resume_fact_guardrails(
            resume_data,
            original_structured_data,
            target_position,
            target_city,
            target_salary,
            allowed_new_project_names,
            allowed_new_skill_names
        )
        resume_data.setdefault('jobIntention', {})
        resume_data['jobIntention']['职位'] = target_position
        if target_city:
            resume_data['jobIntention']['城市'] = target_city
        if target_salary:
            resume_data['jobIntention']['期望薪资'] = target_salary
        if not resume_data.get('personalSummary'):
            resume_data['personalSummary'] = resume_data.get('selfEvaluation', '')[:180]
        
        # 直接更新当前简历，不再新增一条简历记录
        candidate_name = original_resume.get('candidateName', '未知')
        output_filename = f"{resume_id}_{candidate_name}_优化简历.pdf"
        output_path = os.path.join(GENERATED_RESUMES_FOLDER, output_filename)
        
        try:
            # 使用优化后的PDF生成器
            generate_resume_pdf(resume_data, output_path)
            
            # 检查文件是否成功生成
            if not os.path.exists(output_path):
                raise Exception("PDF文件生成失败")
                
        except Exception as pdf_error:
            import traceback
            print(f"PDF生成错误: {traceback.format_exc()}")
            return jsonify({'error': f'PDF生成失败: {str(pdf_error)}'}), 500
        
        # 更新当前简历元数据，避免简历库生成重复记录
        file_size_mb = get_file_size_mb(output_path)
        for item in metadata:
            if item.get('id') == resume_id:
                item.update({
                    'originalName': f"{candidate_name}_优化简历.pdf",
                    'filename': output_filename,
                    'filePath': output_path,
                    'candidateName': candidate_name,
                    'position': target_position,
                    'targetedJob': target_job,
                    'size': round(file_size_mb, 2),
                    'updatedAt': datetime.now().isoformat(),
                    'status': 'active',
                    'sourceSessionId': session_id,
                    'isOptimized': True,
                    'interviewCount': session.get('interviewCount', 1)
                })
                break
        save_metadata(metadata)

        structured_data = load_structured_data()
        structured_data[resume_id] = resume_data
        save_structured_data(structured_data)
        
        # 更新会话信息
        session['generatedResumeId'] = resume_id
        sessions[session_id] = session
        save_interview_sessions(sessions)
        
        # 直接返回PDF文件供下载
        download_name = f"{candidate_name}_优化简历.pdf"
        return send_file(
            output_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/pdf'
        )
        
    except json.JSONDecodeError as e:
        return jsonify({'error': f'简历数据格式错误: {str(e)}'}), 500
    except Exception as e:
        import traceback
        print(f"生成下载错误: {traceback.format_exc()}")
        return jsonify({'error': f'生成失败: {str(e)}'}), 500


@app.route('/api/interview/restart', methods=['POST'])
def restart_interview():
    """基于已有简历重新开始面试训练"""
    data = request.get_json()
    resume_id = data.get('resumeId')
    target_job = normalize_target_job(data.get('targetedJob'))
    
    if not resume_id:
        return jsonify({'error': '请选择简历'}), 400
    
    # 获取简历信息
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == resume_id and r.get('status') == 'active'), None)
    
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 获取简历内容
    contents = load_resume_contents()
    content_data = contents.get(resume_id)
    
    # 如果是生成的优化简历，尝试获取原始简历内容
    if resume.get('isOptimized') and resume.get('sourceResumeId'):
        source_content = contents.get(resume.get('sourceResumeId'))
        if source_content:
            content_data = source_content
    
    resume_content = content_data.get('content') if content_data else None
    
    # 计算面试次数
    sessions = load_interview_sessions()
    interview_count = sum(1 for s in sessions.values() if s.get('resumeId') == resume_id or s.get('sourceResumeId') == resume_id)
    
    resume_position = resume.get('position', '未知')
    interview_position = get_interview_position(resume_position, target_job)

    # 创建面试会话
    session_id = str(uuid.uuid4())
    system_prompt = get_interview_system_prompt(
        resume['candidateName'], 
        interview_position,
        resume_content,
        target_job,
        resume_position
    )
    
    # 增强的开场提示，根据面试次数调整策略
    if interview_count > 0:
        opener_prompt = f"""这是候选人的第{interview_count + 1}次面试训练。
请在之前面试的基础上，从不同角度深入挖掘候选人的能力：
- 询问之前未详细讨论的经历
- 探索新的技术领域和项目经验
- 深入了解软技能和团队协作能力

请开始面试，提出一个新颖的开场问题。"""
    else:
        opener_prompt = f"请开始面试。先明确告诉候选人本次面试岗位是「{interview_position}」，再结合简历和目标岗位要求提出第一个问题"
    
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": opener_prompt}
            ],
            stream=False
        )
        
        ai_message = response.choices[0].message.content
        
        # 保存会话
        sessions[session_id] = {
            'resumeId': resume_id,
            'sourceResumeId': resume.get('sourceResumeId', resume_id),
            'candidateName': resume['candidateName'],
            'position': interview_position,
            'resumePosition': resume_position,
            'targetedJob': target_job,
            'systemPrompt': system_prompt,
            'hasResumeContent': resume_content is not None,
            'messages': [
                {"role": "assistant", "content": ai_message}
            ],
            'startTime': datetime.now().isoformat(),
            'status': 'active',
            'interviewCount': interview_count + 1,
            'isRestart': True
        }
        save_interview_sessions(sessions)
        
        return jsonify({
            'sessionId': session_id,
            'message': ai_message,
            'candidateName': resume['candidateName'],
            'position': interview_position,
            'resumePosition': resume_position,
            'targetedJob': target_job,
            'hasResumeContent': resume_content is not None,
            'interviewCount': interview_count + 1
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'AI服务错误: {str(e)}'}), 500

# ==================== 简历手动编辑功能 ====================

def load_structured_data():
    """从 SQLite 加载简历结构化数据"""
    data = load_app_data('resume_structured_data', {})
    return data if isinstance(data, dict) else {}

def save_structured_data(data):
    """保存简历结构化数据到 SQLite"""
    save_app_data('resume_structured_data', data)

def get_empty_resume_structure(candidate_name="", position=""):
    """获取空的简历数据结构"""
    return {
        "motto": "",
        "personalSummary": "",
        "basicInfo": {
            "姓名": candidate_name,
            "性别": "",
            "年龄": "",
            "籍贯": "",
            "工作年限": "",
            "电话": "",
            "邮箱": ""
        },
        "jobIntention": {
            "职位": position,
            "城市": "",
            "期望薪资": "",
            "到岗": ""
        },
        "education": {
            "时间": "",
            "学校": "",
            "专业": "",
            "专业成绩": "",
            "主修课程": ""
        },
        "workExperience": [],
        "projects": [],
        "skills": {},
        "certificates": [],
        "selfEvaluation": ""
    }

def get_structured_resume_for_optimization(resume_id, resume_meta=None):
    """获取优化简历时可信的原始结构化数据"""
    structured_data = load_structured_data()
    if resume_id in structured_data:
        return copy.deepcopy(structured_data[resume_id])

    source_id = resume_meta.get('sourceResumeId') if resume_meta else None
    if source_id and source_id in structured_data:
        return copy.deepcopy(structured_data[source_id])

    return None

def structured_resume_to_text(resume_data):
    """把结构化简历转成普通文本，供 AI 在缺少 PDF 原文时参考"""
    if not resume_data:
        return ""

    lines = []
    basic = resume_data.get('basicInfo', {})
    job = resume_data.get('jobIntention', {})
    edu = resume_data.get('education', {})

    lines.append("基本信息")
    for key in ['姓名', '年龄', '性别', '籍贯', '工作年限', '电话', '邮箱']:
        if basic.get(key):
            lines.append(f"{key}：{basic.get(key)}")

    lines.append("求职意向")
    for key in ['职位', '城市', '期望薪资', '到岗']:
        if job.get(key):
            lines.append(f"{key}：{job.get(key)}")

    lines.append("教育背景")
    for key in ['时间', '学校', '专业', '专业成绩', '主修课程']:
        if edu.get(key):
            lines.append(f"{key}：{edu.get(key)}")

    if resume_data.get('workExperience'):
        lines.append("工作经历")
        for item in resume_data.get('workExperience', []):
            lines.append(f"{item.get('period', '')} {item.get('company', '')} {item.get('position', '')}".strip())
            for responsibility in item.get('responsibilities', []):
                lines.append(f"- {responsibility}")

    if resume_data.get('projects'):
        lines.append("项目经历")
        for item in resume_data.get('projects', []):
            lines.append(f"{item.get('projectName', '')} {item.get('period', '')} {item.get('position', '')}".strip())
            if item.get('description'):
                lines.append(item.get('description'))
            for responsibility in item.get('responsibilities', []):
                lines.append(f"- {responsibility}")

    skills = resume_data.get('skills', {})
    if skills:
        lines.append("专业技能")
        for name, detail in skills.items():
            if isinstance(detail, dict):
                lines.append(f"{name}：{detail.get('description', '')}")
            else:
                lines.append(f"{name}：{detail}")

    if resume_data.get('certificates'):
        lines.append("荣誉证书")
        for certificate in resume_data.get('certificates', []):
            lines.append(f"- {certificate}")

    if resume_data.get('selfEvaluation'):
        lines.append("自我评价")
        lines.append(resume_data.get('selfEvaluation'))

    return "\n".join(line for line in lines if line)

def merge_project_with_original_fact(original_project, generated_projects):
    """在保留项目名称、时间和角色的前提下使用优化后的表达"""
    original_name = original_project.get('projectName', '')
    matched_project = None
    for item in generated_projects or []:
        if item.get('projectName') == original_name:
            matched_project = item
            break

    if not matched_project:
        return copy.deepcopy(original_project)

    merged = copy.deepcopy(original_project)
    for key in ['description', 'responsibilities', 'highlights']:
        if matched_project.get(key):
            merged[key] = matched_project.get(key)
    return merged

def collect_project_names_from_analysis(value):
    """从面试分析结果中提取明确提到的新项目名称"""
    names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ['projectName', 'name', 'title', '项目名称'] and isinstance(item, str) and item.strip():
                names.add(item.strip())
            else:
                names.update(collect_project_names_from_analysis(item))
    elif isinstance(value, list):
        for item in value:
            names.update(collect_project_names_from_analysis(item))
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if len(text) <= 80:
            names.add(text)
    return names

def merge_projects_with_additions(original_projects, generated_projects, allowed_new_project_names):
    """保留原项目，并追加面试中明确发现的新项目"""
    merged_projects = [
        merge_project_with_original_fact(project, generated_projects)
        for project in original_projects
    ]
    existing_names = {project.get('projectName', '') for project in merged_projects}

    for project in generated_projects or []:
        project_name = project.get('projectName', '').strip()
        if not project_name or project_name in existing_names:
            continue
        if allowed_new_project_names and project_name not in allowed_new_project_names:
            continue
        merged_projects.append(copy.deepcopy(project))
        existing_names.add(project_name)

    return merged_projects

def merge_skills_with_additions(original_skills, generated_skills, allowed_new_skill_names):
    """保留原技能，并追加面试中明确发现的新技能"""
    merged_skills = copy.deepcopy(original_skills or {})
    for skill_name, skill_detail in (generated_skills or {}).items():
        clean_name = str(skill_name).strip()
        if not clean_name or clean_name in merged_skills:
            continue
        if allowed_new_skill_names and clean_name not in allowed_new_skill_names:
            continue
        merged_skills[clean_name] = copy.deepcopy(skill_detail)
    return merged_skills

def apply_resume_fact_guardrails(
    generated_resume,
    original_resume_data,
    target_position,
    target_city='',
    target_salary='',
    allowed_new_project_names=None,
    allowed_new_skill_names=None
):
    """锁定原简历事实字段，防止 AI 编造学校、公司、证书和经历"""
    if not original_resume_data:
        return generated_resume

    safe_resume = copy.deepcopy(generated_resume)

    # 基本信息、教育背景、证书属于事实字段，必须以原简历为准。
    for key in ['basicInfo', 'education', 'certificates']:
        if original_resume_data.get(key) is not None:
            safe_resume[key] = copy.deepcopy(original_resume_data.get(key))

    # 工作经历只能来自原简历；原简历没有工作经历时，不能为贴合岗位而生成虚假公司。
    safe_resume['workExperience'] = copy.deepcopy(original_resume_data.get('workExperience', []))

    allowed_new_project_names = set(allowed_new_project_names or [])
    allowed_new_skill_names = set(allowed_new_skill_names or [])

    # 项目可以优化表达；新项目必须来自面试分析，不能替换或删除原项目。
    original_projects = original_resume_data.get('projects', [])
    generated_projects = generated_resume.get('projects', [])
    safe_resume['projects'] = merge_projects_with_additions(
        original_projects,
        generated_projects,
        allowed_new_project_names
    )

    # 技能只追加面试中发现的新技能，原技能默认保留。
    safe_resume['skills'] = merge_skills_with_additions(
        original_resume_data.get('skills', {}),
        generated_resume.get('skills', {}),
        allowed_new_skill_names
    )

    safe_resume.setdefault('jobIntention', {})
    safe_resume['jobIntention']['职位'] = target_position
    if target_city:
        safe_resume['jobIntention']['城市'] = target_city
    elif original_resume_data.get('jobIntention', {}).get('城市'):
        safe_resume['jobIntention']['城市'] = original_resume_data['jobIntention']['城市']
    if target_salary:
        safe_resume['jobIntention']['期望薪资'] = target_salary
    elif original_resume_data.get('jobIntention', {}).get('期望薪资'):
        safe_resume['jobIntention']['期望薪资'] = original_resume_data['jobIntention']['期望薪资']
    if original_resume_data.get('jobIntention', {}).get('到岗') and not safe_resume['jobIntention'].get('到岗'):
        safe_resume['jobIntention']['到岗'] = original_resume_data['jobIntention']['到岗']

    if not safe_resume.get('motto') and original_resume_data.get('motto'):
        safe_resume['motto'] = original_resume_data['motto']
    if not safe_resume.get('selfEvaluation') and original_resume_data.get('selfEvaluation'):
        safe_resume['selfEvaluation'] = original_resume_data['selfEvaluation']

    return safe_resume

@app.route('/api/resumes/<file_id>/structured-data', methods=['GET'])
def get_resume_structured_data(file_id):
    """获取简历的结构化数据，用于编辑"""
    # 获取简历元数据
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id and r.get('status') == 'active'), None)
    
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 尝试从缓存加载结构化数据
    structured_data = load_structured_data()
    
    if file_id in structured_data:
        return jsonify({'data': structured_data[file_id], 'source': 'cache'}), 200
    
    # 如果没有缓存，检查是否有原始简历内容可以用AI提取
    contents = load_resume_contents()
    content_data = contents.get(file_id)
    
    if content_data and content_data.get('content'):
        original_content = content_data.get('content', '')
        extracted_data = extract_structured_resume_data(
            original_content,
            resume.get('candidateName', ''),
            resume.get('position', '')
        )
        if extracted_data:
            structured_data[file_id] = extracted_data
            save_structured_data(structured_data)
            return jsonify({'data': extracted_data, 'source': 'ai_extracted'}), 200
    
    # 如果都失败了，返回空的结构
    empty_structure = get_empty_resume_structure(
        resume.get('candidateName', ''),
        resume.get('position', '')
    )
    return jsonify({'data': empty_structure, 'source': 'empty'}), 200

@app.route('/api/resumes/<file_id>/structured-data', methods=['PUT'])
def save_resume_structured_data(file_id):
    """保存简历的结构化数据"""
    # 获取简历元数据
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id and r.get('status') == 'active'), None)
    
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': '数据不能为空'}), 400
    
    # 保存结构化数据
    structured_data = load_structured_data()
    structured_data[file_id] = data
    save_structured_data(structured_data)
    
    return jsonify({'message': '保存成功'}), 200

@app.route('/api/resumes/<file_id>/regenerate-pdf', methods=['POST'])
def regenerate_resume_pdf(file_id):
    """根据结构化数据重新生成PDF"""
    # 获取简历元数据
    metadata = load_metadata()
    resume = next((r for r in metadata if r['id'] == file_id and r.get('status') == 'active'), None)
    
    if not resume:
        return jsonify({'error': '简历不存在'}), 404
    
    # 获取结构化数据
    data = request.get_json()
    resume_data = data.get('resumeData') if data else None
    
    if not resume_data:
        # 尝试从缓存加载
        structured_data = load_structured_data()
        resume_data = structured_data.get(file_id)
        
    if not resume_data:
        return jsonify({'error': '没有可用的简历数据'}), 400
    
    # 验证必要字段
    if not resume_data.get('basicInfo'):
        return jsonify({'error': '简历数据不完整，缺少基本信息'}), 400
    
    try:
        # 直接更新当前简历，不再新增一条简历记录
        candidate_name = resume_data.get('basicInfo', {}).get('姓名', resume.get('candidateName', '未知'))
        output_filename = f"{file_id}_{candidate_name}_手动编辑.pdf"
        output_path = os.path.join(GENERATED_RESUMES_FOLDER, output_filename)
        
        # 使用PDF生成器
        generate_resume_pdf(resume_data, output_path)
        
        if not os.path.exists(output_path):
            raise Exception("PDF文件生成失败")
        
        # 更新当前简历元数据，避免简历库生成重复记录
        file_size_mb = get_file_size_mb(output_path)
        for item in metadata:
            if item.get('id') == file_id:
                item.update({
                    'originalName': f"{candidate_name}_手动编辑简历.pdf",
                    'filename': output_filename,
                    'filePath': output_path,
                    'candidateName': candidate_name,
                    'position': resume_data.get('jobIntention', {}).get('职位', resume.get('position', '')),
                    'size': round(file_size_mb, 2),
                    'updatedAt': datetime.now().isoformat(),
                    'status': 'active',
                    'isOptimized': True,
                    'isManuallyEdited': True
                })
                break
        save_metadata(metadata)
        
        # 保存结构化数据到当前简历ID
        structured_data = load_structured_data()
        structured_data[file_id] = resume_data
        save_structured_data(structured_data)
        
        # 直接返回PDF文件供下载
        download_name = f"{candidate_name}_手动编辑简历.pdf"
        return send_file(
            output_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/pdf'
        )
        
    except Exception as e:
        import traceback
        print(f"PDF生成错误: {traceback.format_exc()}")
        return jsonify({'error': f'PDF生成失败: {str(e)}'}), 500


@app.route('/api/interview/<session_id>/generate-report', methods=['POST'])
def generate_interview_report(session_id):
    """根据面试对话生成面试报告"""
    sessions = load_interview_sessions()
    session = sessions.get(session_id)
    
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    
    # 获取面试对话记录
    messages = session.get('messages', [])
    if len(messages) < 2:
        return jsonify({'error': '面试对话太短，无法生成报告'}), 400
    
    # 构建对话文本
    messages_text = "\n".join([
        f"{'面试官' if msg['role'] == 'assistant' else '候选人'}: {msg['content']}"
        for msg in messages
    ])
    
    candidate_name = session.get('candidateName', '未知')
    position = session.get('position', '未知')
    target_job = normalize_target_job(session.get('targetedJob'))
    target_job_context = format_target_job_context(target_job)
    
    # 获取原始简历内容（如果有）
    resume_id = session.get('resumeId')
    resume_content = ""
    if resume_id:
        contents = load_resume_contents()
        content_data = contents.get(resume_id)
        if content_data:
            resume_content = content_data.get('content', '')[:2000]
    
    report_prompt = f"""你是一位资深的面试评估专家。请根据以下面试对话，生成一份专业的面试评估报告。

	## 候选人信息
	- 姓名：{candidate_name}
	- 应聘职位：{position}

## 本次目标岗位
{target_job_context}

	## 原始简历概要
{resume_content if resume_content else '无简历信息'}

## 面试对话记录
{messages_text}

---

请根据以上内容，生成一份结构化的面试评估报告。严格按照以下JSON格式返回（不要添加任何其他内容）：

{{
    "overallScore": 85,
    "recommendation": "推荐/待定/不推荐",
    "summary": "整体评价总结，100-150字",
    "technicalAssessment": {{
        "score": 80,
        "level": "优秀/良好/一般/较弱",
        "highlights": ["技术亮点1", "技术亮点2"],
        "gaps": ["待提升点1"],
        "details": "技术能力详细评估，80-100字"
    }},
    "softSkillsAssessment": {{
        "score": 85,
        "communication": {{"score": 90, "comment": "沟通表达能力评价"}},
        "problemSolving": {{"score": 80, "comment": "问题解决能力评价"}},
        "learning": {{"score": 85, "comment": "学习能力评价"}},
        "teamwork": {{"score": 80, "comment": "团队协作能力评价"}}
    }},
    "projectExperience": {{
        "score": 80,
        "highlights": ["项目亮点1", "项目亮点2"],
        "depth": "项目深度评估描述"
    }},
    "cultureFit": {{
        "score": 75,
        "motivation": "求职动机评估",
        "careerPlan": "职业规划评估"
    }},
    "strengths": ["核心优势1", "核心优势2", "核心优势3"],
    "areasForImprovement": ["改进建议1", "改进建议2"],
    "interviewHighlights": ["面试中的精彩回答或表现1", "精彩表现2"],
    "suggestedQuestions": ["后续面试可深入的问题1", "问题2"]
}}

评分标准：
- 90-100分：表现出色，强烈推荐
- 80-89分：表现良好，推荐
- 70-79分：表现一般，待定
- 60-69分：表现较弱，不太推荐
- 60分以下：表现不佳，不推荐"""

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位专业的面试评估专家，擅长分析面试对话并生成客观、专业的评估报告。请严格按照JSON格式返回结果。"},
                {"role": "user", "content": report_prompt}
            ],
            stream=False
        )
        
        report_text = response.choices[0].message.content
        
        # 解析JSON
        json_match = re.search(r'\{[\s\S]*\}', report_text)
        if json_match:
            try:
                report_data = json.loads(json_match.group())
            except json.JSONDecodeError:
                report_data = {
                    "overallScore": 75,
                    "recommendation": "待定",
                    "summary": report_text[:300],
                    "error": "报告解析失败，显示原始内容"
                }
        else:
            report_data = {
                "overallScore": 75,
                "recommendation": "待定",
                "summary": report_text[:300],
                "error": "报告格式异常"
            }
        
        # 添加元信息
        report_data['candidateName'] = candidate_name
        report_data['position'] = position
        report_data['interviewDate'] = session.get('startTime', datetime.now().isoformat())
        report_data['messageCount'] = len(messages)
        recommended_jobs = generate_recommended_jobs(report_data, session)
        session['report'] = report_data
        session['reportGeneratedAt'] = datetime.now().isoformat()
        session['recommendedJobs'] = recommended_jobs
        sessions[session_id] = session
        save_interview_sessions(sessions)
        
        return jsonify({
            'report': report_data,
            'sessionId': session_id,
            'recommendedJobs': recommended_jobs
        }), 200
        
    except Exception as e:
        import traceback
        print(f"报告生成错误: {traceback.format_exc()}")
        return jsonify({'error': f'报告生成失败: {str(e)}'}), 500


@app.route('/api/interview/<session_id>/report/download', methods=['GET'])
def download_interview_report(session_id):
    """下载已生成的面试报告 PDF"""
    sessions = load_interview_sessions()
    session = sessions.get(session_id)

    if not session:
        return jsonify({'error': '会话不存在'}), 404

    report_data = session.get('report')
    if not report_data:
        return jsonify({'error': '请先生成面试报告，再下载PDF'}), 400

    try:
        pdf_buffer = generate_interview_report_pdf(report_data)
        candidate_name = re.sub(r'[\\/:*?"<>|\s]+', '_', str(report_data.get('candidateName') or '候选人')).strip('_')
        download_name = f"{candidate_name or '候选人'}_面试评估报告.pdf"
        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/pdf'
        )
    except Exception as e:
        import traceback
        print(f"面试报告PDF生成错误: {traceback.format_exc()}")
        return jsonify({'error': f'面试报告PDF生成失败: {str(e)}'}), 500

# ==================== 职位库与意向职业库功能 ====================

LOGO_UPLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend', 'assets', 'company-logos'))
ALLOWED_LOGO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'svg'}
os.makedirs(LOGO_UPLOAD_DIR, exist_ok=True)

def get_job_db_connection():
    """连接岗位相关的本地 SQLite 数据库"""
    return get_db_connection()

def save_job_record(kind, job_data):
    """保存一条真实岗位或自定义岗位"""
    job_id = normalize_job_id(job_data.get('id'))
    now = datetime.now().isoformat()
    payload = json.dumps(job_data, ensure_ascii=False)
    created_at = job_data.get('createdAt') or job_data.get('updatedAt') or now
    updated_at = job_data.get('updatedAt') or job_data.get('createdAt') or now
    with get_job_db_connection() as connection:
        connection.execute(
            '''
            INSERT OR REPLACE INTO job_records (kind, id, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (kind, job_id, payload, created_at, updated_at)
        )

def load_job_records(kind):
    """读取指定类型的岗位列表"""
    with get_job_db_connection() as connection:
        rows = connection.execute(
            'SELECT payload FROM job_records WHERE kind = ? ORDER BY created_at ASC',
            (kind,)
        ).fetchall()
    jobs = []
    for row in rows:
        try:
            jobs.append(json.loads(row['payload']))
        except json.JSONDecodeError:
            continue
    return jobs

def save_candidate_favorite_job_record(resume_id, candidate_name, item):
    """保存一条候选人的意向职业记录"""
    job_id = get_wishlist_item_job_id(item)
    if not resume_id or not job_id:
        return
    payload = json.dumps(item, ensure_ascii=False)
    added_at = item.get('addedAt') or item.get('added_at') or datetime.now().isoformat()
    with get_job_db_connection() as connection:
        connection.execute(
            '''
            INSERT OR REPLACE INTO candidate_favorite_jobs (resume_id, job_id, candidate_name, payload, added_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (str(resume_id), str(job_id), str(candidate_name or '候选人'), payload, added_at)
        )

def load_candidate_job_wishlist(candidate_key):
    """读取指定候选人的意向职业库"""
    with get_job_db_connection() as connection:
        rows = connection.execute(
            '''
            SELECT payload FROM candidate_favorite_jobs
            WHERE candidate_name = ? OR resume_id = ?
            ORDER BY added_at ASC
            ''',
            (str(candidate_key), str(candidate_key))
        ).fetchall()
    wishlist = []
    for row in rows:
        try:
            wishlist.append(json.loads(row['payload']))
        except json.JSONDecodeError:
            continue
    return wishlist

def load_custom_jobs():
    """从 SQLite 加载自定义职位库"""
    return load_job_records('custom')

def save_custom_jobs(data):
    """保存自定义职位库到 SQLite"""
    with get_job_db_connection() as connection:
        connection.execute('DELETE FROM job_records WHERE kind = ?', ('custom',))
    for item in data:
        save_job_record('custom', item)

def load_real_jobs():
    """从 SQLite 加载真实岗位库"""
    return load_job_records('real')

def save_real_jobs(data):
    """保存真实岗位库到 SQLite"""
    with get_job_db_connection() as connection:
        connection.execute('DELETE FROM job_records WHERE kind = ?', ('real',))
    for item in data:
        save_job_record('real', item)

def allowed_logo_file(filename):
    """检查公司图标扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_LOGO_EXTENSIONS

def normalize_job_id(job_id):
    """统一职位ID类型，便于比较"""
    return str(job_id) if job_id is not None else ''

def get_wishlist_item_job_id(item):
    """兼容旧版意向库字段，统一提取职位ID"""
    if not isinstance(item, dict):
        return normalize_job_id(item)
    return normalize_job_id(
        item.get('jobId')
        or item.get('job_id')
        or item.get('id')
        or (item.get('job') or {}).get('id')
    )

initialize_database()

@app.route('/api/custom-jobs', methods=['GET'])
def get_custom_jobs():
    """获取自定义职位列表"""
    return jsonify(load_custom_jobs()), 200

@app.route('/api/real-jobs', methods=['GET'])
def get_real_jobs():
    """获取真实岗位列表"""
    return jsonify(load_real_jobs()), 200

@app.route('/api/real-jobs', methods=['POST'])
def add_real_job():
    """创建真实岗位，字段按招聘网站岗位详情页保存"""
    form = request.form
    required_fields = ['title', 'company', 'category', 'location', 'salary', 'experience', 'education', 'description']
    missing_fields = [field for field in required_fields if not (form.get(field) or '').strip()]
    if missing_fields:
        return jsonify({'error': f"缺少必填字段: {', '.join(missing_fields)}"}), 400

    logo_relative_path = (form.get('logoPath') or '').strip()
    logo_file = request.files.get('logo')
    if logo_file and logo_file.filename:
        if not allowed_logo_file(logo_file.filename):
            return jsonify({'error': '公司图标仅支持 PNG/JPG/JPEG/WEBP/SVG'}), 400
        ext = logo_file.filename.rsplit('.', 1)[1].lower()
        logo_filename = f"real_{uuid.uuid4().hex}.{ext}"
        logo_abs_path = os.path.join(LOGO_UPLOAD_DIR, logo_filename)
        logo_file.save(logo_abs_path)
        logo_relative_path = f"assets/company-logos/{logo_filename}"

    tags_raw = (form.get('tags') or '').strip()
    benefits_raw = (form.get('benefits') or '').strip()
    tags = [tag.strip() for tag in re.split(r'[,\n，、]+', tags_raw) if tag.strip()] if tags_raw else []
    benefits = [tag.strip() for tag in re.split(r'[,\n，、]+', benefits_raw) if tag.strip()] if benefits_raw else []

    real_jobs = load_real_jobs()
    job_id = f"real_{int(datetime.now().timestamp() * 1000)}"
    while any(normalize_job_id(item.get('id')) == normalize_job_id(job_id) for item in real_jobs):
        job_id = f"real_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:4]}"

    job_data = {
        'id': job_id,
        'title': form.get('title', '').strip(),
        'company': form.get('company', '').strip(),
        'category': form.get('category', '').strip(),
        'jobType': (form.get('jobType') or '').strip(),
        'location': form.get('location', '').strip(),
        'salary': form.get('salary', '').strip(),
        'experience': form.get('experience', '').strip(),
        'education': form.get('education', '').strip(),
        'status': (form.get('status') or '招聘中').strip(),
        'source': (form.get('source') or 'Boss直聘').strip(),
        'sourceUrl': (form.get('sourceUrl') or '').strip(),
        'description': form.get('description', '').strip(),
        'responsibilities': (form.get('responsibilities') or '').strip(),
        'requirements': (form.get('requirements') or '').strip(),
        'tags': tags,
        'benefits': benefits,
        'logoPath': logo_relative_path,
        'companyInfo': (form.get('companyInfo') or '').strip(),
        'companyFullName': (form.get('companyFullName') or '').strip(),
        'companySize': (form.get('companySize') or '').strip(),
        'financingStage': (form.get('financingStage') or '').strip(),
        'industry': (form.get('industry') or '').strip(),
        'contactName': (form.get('contactName') or '').strip(),
        'contactRole': (form.get('contactRole') or '').strip(),
        'workAddress': (form.get('workAddress') or '').strip(),
        'updatedAt': datetime.now().isoformat()
    }

    real_jobs.append(job_data)
    save_real_jobs(real_jobs)
    return jsonify({'message': '真实岗位添加成功', 'data': job_data}), 200

@app.route('/api/custom-jobs', methods=['POST'])
def add_custom_job():
    """创建自定义职位（支持上传公司图标）"""
    form = request.form
    required_fields = ['title', 'company', 'category', 'location', 'salary', 'experience', 'education', 'description']
    missing_fields = [field for field in required_fields if not (form.get(field) or '').strip()]
    if missing_fields:
        return jsonify({'error': f"缺少必填字段: {', '.join(missing_fields)}"}), 400

    logo_file = request.files.get('logo')
    if not logo_file or logo_file.filename == '':
        return jsonify({'error': '请上传公司图标'}), 400
    if not allowed_logo_file(logo_file.filename):
        return jsonify({'error': '公司图标仅支持 PNG/JPG/JPEG/WEBP/SVG'}), 400

    ext = logo_file.filename.rsplit('.', 1)[1].lower()
    logo_filename = f"custom_{uuid.uuid4().hex}.{ext}"
    logo_abs_path = os.path.join(LOGO_UPLOAD_DIR, logo_filename)
    logo_file.save(logo_abs_path)
    logo_relative_path = f"assets/company-logos/{logo_filename}"

    tags_raw = (form.get('tags') or '').strip()
    tags = [tag.strip() for tag in re.split(r'[,\n，、]+', tags_raw) if tag.strip()] if tags_raw else []
    if not tags:
        tags = ['自定义职位']

    custom_jobs = load_custom_jobs()
    job_id = int(datetime.now().timestamp() * 1000)
    while any(normalize_job_id(item.get('id')) == normalize_job_id(job_id) for item in custom_jobs):
        job_id += 1

    job_data = {
        'id': job_id,
        'title': form.get('title', '').strip(),
        'company': form.get('company', '').strip(),
        'category': form.get('category', '').strip(),
        'location': form.get('location', '').strip(),
        'salary': form.get('salary', '').strip(),
        'experience': form.get('experience', '').strip(),
        'education': form.get('education', '').strip(),
        'description': form.get('description', '').strip(),
        'tags': tags,
        'logoPath': logo_relative_path,
        'source': 'custom',
        'createdAt': datetime.now().isoformat()
    }

    custom_jobs.append(job_data)
    save_custom_jobs(custom_jobs)
    return jsonify({'message': '职位添加成功', 'data': job_data}), 200

@app.route('/api/favorite-jobs', methods=['GET'])
def get_favorite_jobs():
    """获取意向职业列表"""
    candidate_key = (request.args.get('candidateName') or request.args.get('resumeId') or '').strip()
    wishlist = load_candidate_job_wishlist(candidate_key) if candidate_key else []
    return jsonify(wishlist), 200

@app.route('/api/favorite-jobs', methods=['POST'])
def add_favorite_job():
    """添加职位到意向职业库"""
    data = request.get_json()
    resume_id = str(data.get('resumeId') or '').strip()
    candidate_name = str(data.get('candidateName') or '').strip()
    job_id = data.get('jobId')
    job = data.get('job', {})
    
    if not candidate_name:
        return jsonify({'error': '请先选择候选人'}), 400

    if not job_id:
        return jsonify({'error': '职位ID不能为空'}), 400

    item = {
        'jobId': job_id,
        'job': job,
        'resumeId': resume_id or candidate_name,
        'candidateName': candidate_name,
        'addedAt': datetime.now().isoformat()
    }
    save_candidate_favorite_job_record(candidate_name, item['candidateName'], item)
    return jsonify({'message': '添加成功', 'jobId': job_id}), 200

@app.route('/api/favorite-jobs/<job_id>', methods=['DELETE'])
def remove_favorite_job(job_id):
    """从意向职业库移除职位"""
    candidate_key = (request.args.get('candidateName') or request.args.get('resumeId') or '').strip()
    if not candidate_key:
        return jsonify({'error': '请先选择候选人'}), 400

    normalized_target = normalize_job_id(job_id)
    with get_job_db_connection() as connection:
        cursor = connection.execute(
            '''
            DELETE FROM candidate_favorite_jobs
            WHERE (candidate_name = ? OR resume_id = ?) AND job_id = ?
            ''',
            (str(candidate_key), str(candidate_key), normalized_target)
        )

    if cursor.rowcount == 0:
        return jsonify({'error': '职位不存在'}), 404

    return jsonify({'message': '移除成功', 'jobId': job_id}), 200

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    # 支持通过环境变量注入端口，避免固定端口冲突
    app_port = int(os.getenv('APP_PORT', '5000'))
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=app_port)
