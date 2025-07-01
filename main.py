import os
import requests
import argparse
from packaging.version import parse as parse_version, InvalidVersion
from openai import OpenAI

# --- 配置 ---
# GitHub API 的基础 URL
API_URL = "https://api.github.com"
# 触发分块总结的字符数阈值。保守设置以适应各种模型和prompt。
# 1 token 约等于 4 个英文字符。默认 15000 约等于 3750 tokens。
# 可通过环境变量 MAX_CHARS_FOR_SINGLE_CALL 进行配置。
MAX_CHARS_FOR_SINGLE_CALL = int(os.getenv("MAX_CHARS_FOR_SINGLE_CALL", 15000))


def get_releases(owner: str, repo: str, token: str) -> list | None:
    """从指定的 GitHub 仓库获取所有 Release 的列表。"""
    releases = []
    url = f"{API_URL}/repos/{owner}/{repo}/releases"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    print(f"[*] 正在从 https://github.com/{owner}/{repo} 获取 Release 列表...")

    while url:
        try:
            response = requests.get(url, headers=headers, params={"per_page": 100})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                print(f"[!] 错误: API 返回了非预期的格式。")
                return None
            releases.extend(data)
            url = response.links.get('next', {}).get('url')
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print(f"[!] 错误: 仓库 '{owner}/{repo}' 未找到。")
            elif e.response.status_code in [401, 403]:
                print(f"[!] 错误: GitHub Token 无效或权限不足。")
            else:
                print(f"[!] HTTP 错误: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[!] 网络请求错误: {e}")
            return None

    print(f"[*] 完成！共找到 {len(releases)} 个 Release。")
    return releases


def filter_and_collect_releases(
    releases: list, 
    start_version_str: str | None = None, 
    end_version_str: str | None = None
) -> list[str]:
    """
    根据版本区间过滤 release，并将它们的说明文字收集到一个列表中。
    """
    if not releases:
        return []

    try:
        start_v = parse_version(start_version_str) if start_version_str else None
        end_v = parse_version(end_version_str) if end_version_str else None
    except InvalidVersion as e:
        print(f"[!] 错误: 无效的版本号格式: {e}")
        return []
    
    valid_releases = []
    for rel in releases:
        try:
            rel['version_obj'] = parse_version(rel['tag_name'])
            valid_releases.append(rel)
        except InvalidVersion:
            print(f"[-] 警告: 忽略无法解析的 tag: {rel.get('tag_name', 'N/A')}")

    sorted_releases = sorted(valid_releases, key=lambda r: r['version_obj'], reverse=True)
    
    release_notes_collection = []
    for release in sorted_releases:
        release_v = release['version_obj']
        
        if (start_v and release_v < start_v) or (end_v and release_v > end_v):
            continue
            
        note = f"## 版本: {release['tag_name']} (名称: {release.get('name', 'N/A')})\n"
        note += f"发布于: {release.get('published_at', 'N/A')}\n"
        note += "---\n"
        body = release.get('body')
        note += body.strip() if body and body.strip() else "(此 Release 没有提供说明文字)\n"
        release_notes_collection.append(note)

    print(f"[*] 已筛选出 {len(release_notes_collection)} 个符合条件的 Release 用于处理。")
    return release_notes_collection


def get_ai_summary(client: OpenAI, model_name: str, prompt: str, content: str, stream: bool = False):
    """
    通用的 AI 调用函数，支持流式和非流式两种模式。
    """
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "你是一位资深的软件技术专家和文档工程师。"},
            {"role": "user", "content": prompt.format(content=content)}
        ],
        temperature=0.5,
        stream=stream,
    )
    
    if stream:
        return (chunk.choices[0].delta.content or "" for chunk in response)
    else:
        # print token usage for non-streaming
        if hasattr(response, "usage"):
            usage = response.usage
            print(f"[Token Usage] prompt_tokens: {usage.prompt_tokens}, completion_tokens: {usage.completion_tokens}, total_tokens: {usage.total_tokens}")
        else:
            print("[Token Usage] 无法获取 token 使用信息。")
        return response.choices[0].message.content


def process_ai_summarization(api_key: str, api_base_url: str, model_name: str, notes: list[str]):
    """
    处理 AI 总结的整个流程，包括自动分块和流式输出。
    """
    full_content = "\n\n" + "="*60 + "\n\n".join(notes)
    
    try:
        client = OpenAI(api_key=api_key, base_url=api_base_url)
        
        # 打印标题
        print("\n" + "="*60)
        print(" " * 22 + "AI 生成的发布总结")
        print("="*60 + "\n")

        summary_stream = None

        # 如果内容长度小于阈值，直接进行总结
        if len(full_content) < MAX_CHARS_FOR_SINGLE_CALL:
            print(f"[*] 内容长度适中，将直接进行流式总结...")
            print(f"[*] 使用模型: '{model_name}'")
            prompt = """请根据以下提供的多个 GitHub Release 的说明文字，对这些版本更新内容进行全面、清晰的归纳总结。

请将总结分为以下几个部分（如果某个部分在提供的内容中没有信息，请明确指出“未提及相关信息”）：
1.  **主要新功能 (Major New Features)**
2.  **重要优化和改进 (Key Enhancements & Improvements)**
3.  **修复的关键 Bug (Critical Bug Fixes)**
4.  **重大变更或弃用 (Breaking Changes or Deprecations)**
5.  **总体概述 (Overall Summary)**

请直接开始输出总结内容。

---
待总结的 Release 说明文字如下:
{content}
---
"""
            summary_stream = get_ai_summary(client, model_name, prompt, full_content, stream=True)

        # 如果内容超长，则进行分块总结（Map-Reduce）
        else:
            print(f"\n[!] 内容过长 (约 {len(full_content)} 字符)，将启动分块总结模式...")
            print(f"[*] 使用模型: '{model_name}'")
            
            chunks = []
            current_chunk = []
            current_length = 0
            for note in notes:
                if current_length + len(note) > MAX_CHARS_FOR_SINGLE_CALL and current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk = [note]
                    current_length = len(note)
                else:
                    current_chunk.append(note)
                    current_length += len(note)
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))

            print(f"[*] 已将内容分割成 {len(chunks)} 个块进行处理。")
            
            # Map 步骤: 对每个块进行总结 (非流式)
            partial_summaries = []
            chunk_prompt = """以下是一系列 GitHub Release 说明文字的一部分。请你只针对当前提供的内容，总结其核心要点，包括新功能、改进和 Bug 修复。
你的总结将作为后续生成最终报告的素材，因此请确保信息的准确和简洁。

---
当前块的内容如下:
{content}
---
"""
            for i, chunk in enumerate(chunks):
                print(f"[*] 正在总结第 {i+1}/{len(chunks)} 块...")
                # Map 步骤不需要流式，我们需要完整内容进行 Reduce
                partial_summary = get_ai_summary(client, model_name, chunk_prompt, chunk, stream=False)
                partial_summaries.append(partial_summary)

            print("[*] 所有块已总结完毕，正在进行最终汇总和流式输出...")
            
            # Reduce 步骤: 合并所有部分摘要，生成最终总结 (流式)
            combined_summary = "\n\n---\n\n".join(partial_summaries)
            final_prompt = """你收到了多份关于一个软件版本区间的“部分摘要”。请你将这些摘要整合起来，生成一份单一、连贯、全面的最终总结报告。
请消除重复信息，并按照以下结构组织最终报告：
1.  **主要新功能 (Major New Features)**
2.  **重要优化和改进 (Key Enhancements & Improvements)**
3.  **修复的关键 Bug (Critical Bug Fixes)**
4.  **重大变更或弃用 (Breaking Changes or Deprecations)**
5.  **总体概述 (Overall Summary)**

如果某个部分没有信息，请明确指出“未提及相关信息”。

---
待整合的部分摘要如下:
{content}
---
"""
            summary_stream = get_ai_summary(client, model_name, final_prompt, combined_summary, stream=True)

        # 处理流式输出
        if summary_stream:
            for chunk in summary_stream:
                print(chunk, end="", flush=True)
        
        print("\n\n" + "="*60 + "\n")

    except Exception as e:
        print(f"\n[!] AI 总结失败: {e}")
        print("[!] 请检查您的 API Key、Base URL 和模型名称是否正确，以及网络连接是否正常。")

def print_raw_releases(notes: list[str]):
    """直接打印原始的、筛选后的 release 说明。"""
    if not notes:
        print("[*] 在指定的版本区间内没有找到任何 Release。")
        return
        
    print("\n" + "="*60)
    print(" " * 20 + "符合条件的 Release 说明")
    print("="*60)
    print("\n\n" + "="*60 + "\n" + "\n\n".join(notes))


def main():
    """主函数，处理命令行参数并协调整个流程。"""
    parser = argparse.ArgumentParser(
        description="从 GitHub 仓库获取 Release 说明，并可选择使用 AI进行总结。",
        epilog="示例: python get_releases.py microsoft/vscode --start 1.88.0 --summarize",
        formatter_class=argparse.RawTextHelpFormatter
    )
    # GitHub 相关参数
    parser.add_argument("repo", help="目标 GitHub 仓库，格式为 'owner/repo' (例如: 'microsoft/vscode')")
    parser.add_argument("--start", help="起始版本号 (包含此版本)。")
    parser.add_argument("--end", help="结束版本号 (包含此版本)。")
    parser.add_argument("--token", help="GitHub 个人访问令牌。默认为环境变量 'GITHUB_TOKEN'。")
    
    # AI 总结相关参数
    parser.add_argument("--summarize", action="store_true", help="启用 AI 总结功能。启用后将同时展示原文和总结。")
    parser.add_argument("--ai-api-key", help="OpenAI 兼容 API 的密钥。默认为环境变量 'OPENAI_API_KEY'。")
    parser.add_argument("--ai-api-base", help="OpenAI 兼容 API 的 Base URL。默认为环境变量 'OPENAI_API_BASE'。")
    parser.add_argument("--model", help="要使用的 AI 模型名称。默认为环境变量 'OPENAI_MODEL_NAME'，若也未设置则为 'gpt-4o-mini'。")

    args = parser.parse_args()
    
    try:
        owner, repo_name = args.repo.split('/')
    except ValueError:
        print("[!] 错误: 仓库格式不正确，请使用 'owner/repo' 的格式。")
        return

    token = args.token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("[!] 错误: 缺少 GitHub Token。请使用 --token 或设置 'GITHUB_TOKEN' 环境变量。")
        return

    # 1. 获取所有 releases
    all_releases = get_releases(owner, repo_name, token)
    if all_releases is None:
        return

    # 2. 根据版本区间过滤并收集说明文字
    release_notes = filter_and_collect_releases(all_releases, args.start, args.end)

    # 3. 打印原始发布说明
    print_raw_releases(release_notes)

    # 4. 如果启用，则进行 AI 总结
    if args.summarize:
        if not release_notes:
            # print_raw_releases 已经打印过没有找到 release 的信息，这里安静退出即可。
            return
            
        api_key = args.ai_api_key or os.getenv("OPENAI_API_KEY")
        api_base = args.ai_api_base or os.getenv("OPENAI_API_BASE")
        model_name = args.model or os.getenv("OPENAI_MODEL_NAME") or "gpt-4o-mini"
        
        if not api_key or not api_base:
            print("[!] 错误: 缺少 AI 配置。请使用 --ai-api-key 和 --ai-api-base 参数，或设置 'OPENAI_API_KEY' 和 'OPENAI_API_BASE' 环境变量。")
            return
        process_ai_summarization(api_key, api_base, model_name, release_notes)

if __name__ == "__main__":
    main()
