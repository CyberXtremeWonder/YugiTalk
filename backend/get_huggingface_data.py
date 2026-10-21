from pathlib import Path

import datasets

# 显式使用项目磁盘的语料缓存，不依赖启动终端是否加载环境变量。
cache_dir = Path(__file__).resolve().parents[1] / ".cache/huggingface/datasets"
lang = 'zh'  # or any of the 16 languages
miracl = datasets.load_dataset(
    'miracl/miracl',
    lang,
    trust_remote_code=True,
    cache_dir=str(cache_dir),
)

# training set:
# print(miracl['train'])
for data in miracl['train']:  # or 'dev', 'testA'
    query_id = data['query_id']
    query = data['query']
    positive_passages = data['positive_passages']
    negative_passages = data['negative_passages']
    print(positive_passages)
    print(negative_passages)
#
#     for entry in positive_passages:  # OR 'negative_passages'
#         docid = entry['docid']
#         title = entry['title']
#         text = entry['text']

