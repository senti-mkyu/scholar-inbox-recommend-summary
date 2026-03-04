import os
import re
import datetime
import requests
import base64
import time
from pydantic import BaseModel, Field
from typing import List
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
from google.genai import errors
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

def exists(v):
    if v is not None:
        return True
    else:
        return False

def get_html(url):
    with sync_playwright() as p:
        # 브라우저 실행
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # URL 접속
        page.goto(url)
        
        # 페이지가 완전히 로드될 때까지 대기 (필요 시 특정 요소 대기)
        page.wait_for_load_state("networkidle")
        
        # 전체 HTML 가져오기
        html = page.content()
        print("동적 HTML 수집 완료")
        
        browser.close()
        return html

def extract_arxiv_links(html_data):
    # ArXiv HTML 페이지 패턴 정의 (숫자와 점이 포함된 형식)
    # xxxx.xxxxx 또는 xxxx.xxxxxx 형태를 모두 잡기 위한 정규식
    pattern = r'https://arxiv\.org/html/\d{4}\.\d{4,6}(?:v\d+)?'
    
    # 패턴과 일치하는 모든 링크 찾기
    links = re.findall(pattern, html_data)
    
    # 중복 제거 (set 사용 후 다시 list로 변환)
    unique_links = list(dict.fromkeys(links))
    
    return unique_links

def get_valid_arxiv_contents(link):
    valid_contents = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(link, headers=headers)
        if response.status_code != 200:
            print(f"접속 불가 스킵: {link}")
            return False

        # 1. HTML 가용성 확인 로직
        # 페이지 전체 텍스트에서 해당 문구가 있는지 검사합니다.
        if "HTML is not available for the source" in response.text:
            print(f"HTML 미지원 논문 스킵: {link}")
            return False

        # 2. 정상적인 본문 파싱 (예: <article> 태그나 본문 영역 추출)
        soup = BeautifulSoup(response.text, 'html.parser')
        # ArXiv HTML5 버전은 보통 <article>이나 <section> 태그를 사용합니다.
        content = soup.get_text(separator=' ', strip=True)
        time.sleep(1)

    except Exception as e:
        print(f"에러 발생 ({link}): {e}")
            
    return content

# 1. 권한 설정
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    creds = None
    # token.json은 사용자의 인증 정보를 저장합니다.
    if os.environ.get('GMAIL_TOKEN'):
        import json
        token_data = json.loads(os.environ.get('GMAIL_TOKEN'))
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    # try:
    #     creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    # except:
    #     pass

    # if not creds or not creds.valid:
    #     if creds and creds.expired and creds.refresh_token:
    #         creds.refresh(Request())
    #     else:
    #         flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    #         creds = flow.run_local_server(port=0)
    #     with open('token.json', 'w') as token:
    #         token.write(creds.to_json())
    
    return build('gmail', 'v1', credentials=creds)

def fetch_sha_key(target_date=None):
    service = get_gmail_service()
    
    # 날짜 지정 (기본값은 오늘)
    if target_date is None:
        target_date = datetime.date.today()

    # Gmail 쿼리 형식: YYYY/MM/DD
    # 오늘 하루의 메일을 다 잡기 위해 '어제 이후 ~ 내일 이전'으로 설정
    after_date = (target_date).strftime('%Y/%m/%d')
    before_date = (target_date + datetime.timedelta(days=1)).strftime('%Y/%m/%d')
    
    
    # Gmail 쿼리: 발신자와 날짜 기준 필터링
    query = f"from:Scholar Inbox after:{after_date} before:{before_date}"
    print(f"검색 쿼리: {query}")
    
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print(f"{target_date} 에 수신된 Scholar Inbox 메일이 없습니다.")
        return None

    for msg in messages:
        message = service.users().messages().get(userId='me', id=msg['id']).execute()
        
        # 메일 본문 디코딩
        payload = message.get('payload')
        parts = payload.get('parts', [])
        body = ""
        
        if not parts: # 단일 파트 메일인 경우
            body = payload.get('body').get('data')
        else:
            for part in parts:
                if part.get('mimeType') == 'text/html':
                    body = part.get('body').get('data')
                    break
        
        if body:
            decoded_body = base64.urlsafe_b64decode(body).decode('utf-8')
            
            # sha_key 추출을 위한 정규표현식
            # 예: sha_key=df51d0165356d7c7c40dca6c6a1dbda6a6ee0de3
            if decoded_body:
                # sha_key가 포함된 전체 URL을 찾는 정규표현식
                # 공백이나 따옴표가 나오기 전까지의 모든 문자열을 가져옵니다.
                url_pattern = r'https://www\.scholar\-inbox\.com/login\?sha_key=[a-f0-9]+&?date=[0-9\-]*'
                match = re.search(url_pattern, decoded_body)
                
                if match:
                    full_url = match.group(0)
                    # 만약 &amp; 형태로 포함되어 있다면 &로 치환 (Playwright 접속을 위함)
                    full_url = full_url.replace('&amp;', '&')
                    print(f"URL: {full_url}")
                    return full_url

    return None

def download_images(page, base_url, img_dir):
    """HTML 구조에 최적화된 이미지 다운로드 함수"""
    downloaded_paths = []
    # ltx_figure 클래스를 가진 모든 요소를 찾음
    figures = page.query_selector_all("figure.ltx_figure")
    paper_id = base_url.split("/")[-1]
    base_url = "/".join(base_url.split("/")[:-1])

    for idx, fig in enumerate(figures):
        img_tag = fig.query_selector("img.ltx_graphics")
        if not img_tag: continue

        src = img_tag.get_attribute("src") # 예: x1.png
        full_img_url = base_url + '/' + src
        
        caption_tag = fig.query_selector("figcaption")
        caption = caption_tag.inner_text() if caption_tag else f"Figure {idx+1}"
        
        img_filename = f"{paper_id}_fig{idx}.png"
        save_path = os.path.join(img_dir, img_filename)
        
        try:
            img_data = requests.get(full_img_url).content
            with open(save_path, 'wb') as f:
                f.write(img_data)
            # downloaded_paths.append(save_path)
            downloaded_paths.append({"path": save_path, "caption": caption})
        except Exception as e:
            continue
            
    return downloaded_paths

def parse_arxiv_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # 1. 초록(Abstract) 추출
    # ArXiv HTML은 보통 <section id="S1"> 또는 .ltx_abstract에 초록이 담깁니다.
    abstract_section = soup.find('section', id='S1') or soup.select_one('.ltx_abstract')
    abstract_text = ""
    if abstract_section:
        # 제목 제외하고 본문 문단만 추출
        paras = abstract_section.find_all('p', class_='ltx_p')
        abstract_text = "\n".join([p.get_text(strip=True) for p in paras])

    # 2. 본문 섹션(Methods, Results 등) 추출
    sections_data = {}
    sections = soup.find_all('section', class_='ltx_section')
    
    for section in sections:
        title_tag = section.find(['h2', 'h3'], class_='ltx_title')
        if not title_tag:
            continue
            
        title = title_tag.get_text(strip=True)
        # 핵심 키워드(Method, Experiment, Result)가 포함된 섹션만 필터링
        method_keywords = [
            "METHOD", "APPROACH", "ARCHITECTURE", "SYSTEM", "DESIGN", 
            "PROPOSED", "MODEL", "FORMULATION", "ALGORITHM", "TECHNIQUE", "METHODOLOGY"
        ]
        
        if any(keyword in title.upper() for keyword in method_keywords):
            content_paras = section.find_all('p', class_='ltx_p')
            sections_data[title] = "\n".join([p.get_text(strip=True) for p in content_paras])
    
    return abstract_text, sections_data


class PaperReport(BaseModel):
    title: str = Field(description="논문의 제목")
    abstract_org: str = Field(description="초록 내용의 원문")
    abstract_ko: str = Field(description="초록 내용을 누락 없이 한국어로 전체 번역")
    methods_results: List[str] = Field(description="방법론과 실험 결과를 핵심 위주로 요약한 리스트")

def summarize_paper_with_gemini(abstract, sections, max_retries=5):
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    model_name = os.getenv("GEMINI_MODEL_NAME")
    
    # 본문 데이터 구성 (추출된 섹션들을 하나의 문자열로 결합)
    method_context = "\n\n".join([f"[{title}]\n{content}" for title, content in sections.items()])
    
    # 1. 시스템 지침 설정: 양식 고정 및 페르소나 부여
    system_instr = "당신은 AI 분야 전문 연구원입니다. 주어진 논문 데이터를 분석하여 구조화된 형식으로 반환하세요."

    # 2. 메인 프롬프트 구성
    user_prompt = f"""
    [Abstract Raw]
    {abstract}
    
    [Methods & Results Raw]
    {method_context}
    """


    for i in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_prompt,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': PaperReport, # 정의한 구조체 주입
                    'system_instruction': system_instr,
                    'temperature': 0.2, # 일관된 양식을 위해 낮은 온도 설정
                }
            )
            if response.parsed:
                return response.parsed, response.usage_metadata
            else:
                return None, None
        except errors.ServerError as e:
            # 503 에러일 경우 재시도
            if "503" in str(e) and i < max_retries - 1:
                wait_time = (i + 1) * 10  # 10초, 20초 순으로 대기 시간 증가
                print(f"⚠️ 서버 부하 발생(503). {wait_time}초 후 다시 시도합니다... ({i+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            return None, None # 재시도 횟수를 초과하면 에러 발생
        
  
def process_arxiv_page(page, url, img_dir):
    page.goto(url)
    if "HTML is not available" in page.content():
        return None

    title = page.title()
    # 1. 이미지 다운로드 (패턴 기반)
    img_dir = os.path.join(img_dir, str(url).split("/")[-1])
    os.makedirs(img_dir, exist_ok=True)
    local_images = download_images(page, url, img_dir)
    

    # 2. 텍스트 추출 및 Gemini 처리
    # print(page.content())

    
    abstract_text, sections_data = parse_arxiv_html(page.content())

    report, usage = summarize_paper_with_gemini(abstract_text, sections_data)

    return {
        "title": title,
        "url": url,
        "content": report,
        "images": local_images,
        "usage": usage,
        "img_dir": img_dir
    }

def save_report_to_markdown(file_name, data):
    """
    구조체 데이터와 이미지 정보를 결합하여 마크다운 파일로 저장합니다.
    """
    figures = data['images']
    usage_metadata = data['usage']
    report_obj = data['content']

    if not report_obj:
        report_obj = PaperReport(
            title=data['title'],
            abstract_org="Gemini API Call에 실패했습니다. 직접 들어가서 읽으세요 ㅠㅠ",
            abstract_ko="Gemini API Call에 실패했습니다. 직접 들어가서 읽으세요 ㅠㅠ",
            methods_results=["Gemini API Call에 실패했습니다. 직접 들어가서 읽으세요 ㅠㅠ"],
        )
    img_dir = data['img_dir']
    # 1. 마크다운 텍스트 구성 (구조체 데이터 활용)
    md_text = f"# 📚 {data['title']}\n\n"

    md_text += f"🚀 URL: {data['url']}\n\n"
    
    md_text += f"## 🌏 Abstract (원문)\n{report_obj.abstract_org}\n"
    md_text += f"## 🌏 Abstract (번역)\n{report_obj.abstract_ko}\n\n"
    
    md_text += f"## 🔍 Methods & Results\n"
    for point in report_obj.methods_results:
        md_text += f"- {point}\n"
    
    # 2. 이미지 섹션 추가
    if figures:
        md_text += f"\n## 🖼 Figures\n"
        for fig in figures:
            # 이미지 파일명만 추출하여 상대 경로 생성
            img_filename = os.path.basename(fig['path'])
            img_relative_path = f"../{img_dir}/{img_filename}"
            
            md_text += f"![{fig['caption']}]({img_relative_path})\n"
            md_text += f"*{fig['caption']}*\n\n"
    
    # 3. 토큰 정보 및 푸터
    md_text += f"\n---\n**Usage Info**: {usage_metadata.total_token_count} tokens used."
    md_text += f"\n**Generated at**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    md_text += f"\n\n---\n\n" # 이 부분이 논문 사이를 나누는 구분선입니다.

    # 4. 파일 쓰기 (기존 내용이 있으면 이어서 작성)
    mode = 'a' if os.path.exists(file_name) else 'w'
    with open(file_name, mode, encoding="utf-8") as f:
        f.write(md_text)
        f.write("\n\n") # 논문 간 구분선

    print(f"✅ 리포트가 저장되었습니다: {data['title']} --> {file_name}")

def update_main_readme():
    """
    레포지토리 내의 날짜별 마크다운 파일들을 최신순으로 정렬하여 README.md를 업데이트합니다.
    """
    # 1. 날짜 형식(yyyy-mm-dd.md)의 파일만 골라내기
    md_files = [os.path.join("./markdowns", f) for f in os.listdir('./markdowns') if re.match(r'\d{4}-\d{2}-\d{2}\.md', f)]
    # 2. 최신순 정렬 (내림차순)
    md_files.sort(reverse=True)

    # 3. 링크 리스트 생성
    face = "## 📚 요약 리포트 목록\n\n"
    link_list = ""
    for file in md_files:
        date_str = file.split("/")[-1].replace('.md', '')
        link_list += f"- [{date_str} 논문 요약 리포트](./{file})\n"

    # 4. README.md 파일 업데이트 (특정 태그 사이의 내용만 교체)
    readme_path = "README.md"
    
    header_text = "# 🎙️ Speech Synthesis Paper Archive\n음성 합성 및 AI 최신 논문을 Gemini가 요약해주는 저장소입니다.\n\n"
    new_content = header_text + face + f"{link_list}"

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    
    print("✅ README.md 리스트 업데이트 완료")

def main(y=None, m=None, d=None):
    # login_url은 이전 Gmail API 코드로 가져온다고 가정
    # 특정 날짜를 지정하고 싶다면: fetch_sha_key('2026/02/20')
    if exists(y) and exists(m) and exists(d):
        today = f"{y}-{m:02d}-{d:02d}"
        key = datetime.date(year=y, month=m, day=d) 
    else:
        today = datetime.date.today().strftime('%Y-%m-%d')
        key = None

    login_url = fetch_sha_key(key)
    if not login_url:
        return False

    
    img_dir = f"images/{today}"
    md_dir = f"markdowns/"
    md_fp = os.path.join(md_dir, f"{today}.md")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(md_dir, exist_ok=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(login_url)
        # 페이지가 완전히 로드될 때까지 대기 (필요 시 특정 요소 대기)
        page.wait_for_load_state("networkidle")
        arxiv_links = extract_arxiv_links(page.content())
        
        md_content = f"# 🚩 ({today}) Scholar Inbox 추천 논문 \n\n"
        with open(os.path.join(md_dir, f"{today}.md"), "w", encoding="utf-8") as f:
            f.write(md_content)
        
        for link in arxiv_links:
            data = process_arxiv_page(page, link, img_dir)
            if not exists(data):
                continue
            save_report_to_markdown(md_fp, data)
        browser.close()
    return True
    

if __name__ == "__main__":
    # start_date_str = '2026-03-03'
    # end_date_str = '2026-03-04'
    # start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d')
    # end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d')

    # current_date = start_date
    # while current_date <= end_date:
    #     y = current_date.year
    #     m = current_date.month
    #     d = current_date.day
    #     current_date += datetime.timedelta(days=1)

    #     result = main(y=y, m=m, d=d)

    result = main()
    if result:
        update_main_readme()
    else:
        print("No Paper Today.")