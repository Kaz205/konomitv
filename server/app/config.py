
import asyncio
import concurrent.futures
import platform
import psutil
import re
import requests
import ruamel.yaml
import subprocess
import sys
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    confloat,
    DirectoryPath,
    Field,
    FilePath,
    PositiveInt,
    ValidationError,
    validator,
)
from pydantic.networks import stricturl
from pathlib import Path
from typing import Any, cast, Literal

from app.constants import (
    API_REQUEST_HEADERS,
    BASE_DIR,
    LIBRARY_PATH,
)


# クライアント設定を表す Pydantic モデル (クライアント設定同期用 API で利用)
# デバイス間で同期するとかえって面倒なことになりそうな設定は除外されている
# 詳細は client/src/services/Settings.ts と client/src/store/SettingsStore.ts を参照

class ClientSettings(BaseModel):
    pinned_channel_ids: list[str] = Field([])
    # showed_panel_last_time: 同期無効
    # selected_twitter_account_id: 同期無効
    saved_twitter_hashtags: list[str] = Field([])
    # tv_streaming_quality: 同期無効
    # tv_data_saver_mode: 同期無効
    # tv_low_latency_mode: 同期無効
    panel_display_state: Literal['RestorePreviousState', 'AlwaysDisplay', 'AlwaysFold'] = Field('RestorePreviousState')
    tv_panel_active_tab: Literal['Program', 'Channel', 'Comment', 'Twitter'] = Field('Program')
    tv_channel_selection_requires_alt_key: bool = Field(False)
    caption_font: str = Field('Windows TV MaruGothic')
    always_border_caption_text: bool = Field(True)
    specify_caption_opacity: bool = Field(False)
    caption_opacity: float = Field(1.0)
    tv_show_superimpose: bool = Field(True)
    # tv_show_data_broadcasting: 同期無効
    # capture_copy_to_clipboard: 同期無効
    capture_save_mode: Literal['Browser', 'UploadServer', 'Both'] = Field('UploadServer')
    capture_caption_mode: Literal['VideoOnly', 'CompositingCaption', 'Both'] = Field('Both')
    # sync_settings: 同期無効
    comment_speed_rate: float = Field(1)
    comment_font_size: int = Field(34)
    close_comment_form_after_sending: bool = Field(True)
    muted_comment_keywords: list[dict[str, str]] = Field([])
    muted_niconico_user_ids: list[str] = Field([])
    mute_vulgar_comments: bool = Field(True)
    mute_abusive_discriminatory_prejudiced_comments: bool = Field(True)
    mute_big_size_comments: bool = Field(True)
    mute_fixed_comments: bool = Field(False)
    mute_colored_comments: bool = Field(False)
    mute_consecutive_same_characters_comments: bool = Field(False)
    fold_panel_after_sending_tweet: bool = Field(False)
    reset_hashtag_when_program_switches: bool = Field(True)
    auto_add_watching_channel_hashtag: bool = Field(True)
    twitter_active_tab: Literal['Search', 'Timeline', 'Capture'] = Field('Capture')
    tweet_hashtag_position: Literal['Prepend', 'Append', 'PrependWithLineBreak', 'AppendWithLineBreak'] = Field('Append')
    tweet_capture_watermark_position: Literal['None', 'TopLeft', 'TopRight', 'BottomLeft', 'BottomRight'] = Field('None')


# サーバー設定を表す Pydantic モデル
# config.yaml のバリデーションは設定データをこの Pydantic モデルに通すことで行う

class _ServerSettingsGeneral(BaseModel):
    backend: Literal['EDCB', 'Mirakurun']
    edcb_url: stricturl(allowed_schemes={'tcp'}, tld_required=False)  # type: ignore
    mirakurun_url: AnyHttpUrl
    encoder: Literal['FFmpeg', 'QSVEncC', 'NVEncC', 'VCEEncC', 'rkmppenc']
    program_update_interval: confloat(ge=0.1)  # type: ignore
    debug: bool
    debug_encoder: bool

    @validator('edcb_url')
    def validate_edcb_url(cls, edcb_url: str, values: dict[str, Any]) -> str:
        # EDCB バックエンドの接続確認
        if values.get('backend') == 'EDCB':
            # 循環参照を避けるために遅延インポート
            from app.utils.EDCB import CtrlCmdUtil
            from app.utils.EDCB import EDCBUtil
            # edcb_url を明示的に指定
            ## edcb_url を省略すると内部で再帰的に LoadConfig() が呼ばれてしまい RecursionError が発生する
            edcb_host = EDCBUtil.getEDCBHost(edcb_url)
            edcb_port = EDCBUtil.getEDCBPort(edcb_url)
            # ホスト名またはポートが指定されていない
            if ((edcb_host is None) or (edcb_port is None and edcb_host != 'edcb-namedpipe')):
                raise ValueError(
                    'URL 内にホスト名またはポートが指定されていません。\n'
                    'EDCB の URL を間違えている可能性があります。'
                )
            # サービス一覧が取得できるか試してみる
            ## RecursionError 回避のために edcb_url を明示的に指定
            ## ThreadPoolExecutor 上で実行し、自動リロードモード時に発生するイベントループ周りの謎エラーを回避する
            edcb = CtrlCmdUtil(edcb_url)
            edcb.setConnectTimeOutSec(5)  # 5秒後にタイムアウト
            with concurrent.futures.ThreadPoolExecutor(1) as executor:
                result = executor.submit(asyncio.run, edcb.sendEnumService()).result()
            if result is None:
                raise ValueError(
                    f'EDCB ({edcb_url}/) にアクセスできませんでした。\n'
                    'EDCB が起動していないか、URL を間違えている可能性があります。'
                )
            from app.utils import Logging
            Logging.info(f'Backend: EDCB ({edcb_url}/)')
        return edcb_url

    @validator('mirakurun_url')
    def validate_mirakurun_url(cls, mirakurun_url: str, values: dict[str, Any]) -> str:
        # Mirakurun バックエンドの接続確認
        if values.get('backend') == 'Mirakurun':
            # 試しにリクエストを送り、200 (OK) が返ってきたときだけ有効な URL とみなす
            try:
                response = requests.get(
                    url = f'{mirakurun_url}/api/version',
                    headers = API_REQUEST_HEADERS,
                    timeout = 20,  # 久々のアクセスだとなぜか時間がかかることがあるため、ここだけタイムアウトを長めに設定
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                raise ValueError(
                    f'Mirakurun ({mirakurun_url}/) にアクセスできませんでした。\n'
                    'Mirakurun が起動していないか、URL を間違えている可能性があります。'
                )
            try:
                response_json = response.json()
                if response.status_code != 200 or response_json.get('current') is None:
                    raise requests.exceptions.JSONDecodeError()
            except requests.exceptions.JSONDecodeError:
                raise ValueError(
                    f'{mirakurun_url}/ は Mirakurun の URL ではありません。\n'
                    'Mirakurun の URL を間違えている可能性があります。'
                )
            from app.utils import Logging
            Logging.info(f'Backend: Mirakurun {response_json.get("current")} ({mirakurun_url}/)')
        return mirakurun_url

    @validator('encoder')
    def validate_encoder(cls, encoder: str) -> str:
        from app.utils import Logging
        current_arch = platform.machine()
        # x64 なのにエンコーダーとして rkmppenc が指定されている場合
        if current_arch in ['AMD64', 'x86_64'] and encoder == 'rkmppenc':
            raise ValueError(
                'x64 アーキテクチャでは rkmppenc は使用できません。\n'
                '利用するエンコーダーを FFmpeg・QSVEncC・NVEncC・VCEEncC のいずれかに変更してください。'
            )
        # arm64 なのにエンコーダーとして QSVEncC・NVEncC・VCEEncC が指定されている場合
        if current_arch == 'aarch64' and encoder in ['QSVEncC', 'NVEncC', 'VCEEncC']:
            raise ValueError(
                'arm64 アーキテクチャでは QSVEncC・NVEncC・VCEEncC は使用できません。\n'
                '利用するエンコーダーを FFmpeg・rkmppenc のいずれかに変更してください。'
            )
        # HWEncC が指定されているときのみ、--check-hw でハードウェアエンコーダーが利用できるかをチェック
        ## もし利用可能なら標準出力に "H.264/AVC" という文字列が出力されるので、それで判定する
        if encoder != 'FFmpeg':
            result = subprocess.run(
                [LIBRARY_PATH[encoder], '--check-hw'],
                stdout = subprocess.PIPE,
                stderr = subprocess.DEVNULL,
            )
            result_stdout = result.stdout.decode('utf-8')
            result_stdout = '\n'.join([line for line in result_stdout.split('\n') if 'reader:' not in line])
            if 'unavailable.' in result_stdout:
                raise ValueError(
                    f'お使いの環境では {encoder} がサポートされていないため、KonomiTV を起動できません。\n'
                    f'別のエンコーダーを選択するか、{encoder} の動作環境を整備してください。'
                )
            # H.265/HEVC に対応していない環境では、通信節約モードが利用できない旨を出力する
            if 'H.265/HEVC' not in result_stdout:
                Logging.warning(f'お使いの環境では {encoder} での H.265/HEVC エンコードがサポートされていないため、通信節約モードは利用できません。')
        # エンコーダーのバージョン情報を取得する
        ## バージョン情報は出力の1行目にある
        result = subprocess.run(
            [LIBRARY_PATH[encoder], '--version'],
            stdout = subprocess.PIPE,
            stderr = subprocess.STDOUT,
        )
        encoder_version = result.stdout.decode('utf-8').split('\n')[0]
        ## Copyright (FFmpeg) と by rigaya (HWEncC) 以降の文字列を削除
        encoder_version = re.sub(r' Copyright.*$', '', encoder_version)
        encoder_version = re.sub(r' by rigaya.*$', '', encoder_version)
        encoder_version = encoder_version.replace('ffmpeg', 'FFmpeg').strip()
        Logging.info(f'Encoder: {encoder_version}')
        return encoder

class _ServerSettingsServer(BaseModel):
    port: PositiveInt
    custom_https_certificate: FilePath | None
    custom_https_private_key: FilePath | None

    @validator('port')
    def validate_port(cls, port: int) -> int:
        # リッスンするポート番号が 1024 ~ 65525 の間に収まっているかをチェック
        if port < 1024 or port > 65525:
            raise ValueError(
                'ポート番号の設定が不正なため、KonomiTV を起動できません。\n'
                '設定したポート番号が 1024 ~ 65525 (65535 ではない) の間に収まっているかを確認してください。'
            )
        # 使用中のポートを取得
        # ref: https://qiita.com/skokado/items/6e76762c68866d73570b
        current_process = psutil.Process()
        used_ports: list[int] = []
        for conn in psutil.net_connections():
            if conn.status == 'LISTEN':
                if conn.pid is None:
                    continue
                # 自分自身のプロセスは除外
                ## サーバーの起動中に再度バリデーションが実行された際に、ポートが使用中と判定されてしまうのを防ぐためのもの
                ## 自動リロードモードでの reloader process や Akebi は KonomiTV サーバーの子プロセスになるので、
                ## プロセスの親プロセスの PID が一致するかもチェックする
                process = psutil.Process(conn.pid)
                if ((process.pid == current_process.pid) or
                    (process.pid == current_process.ppid()) or
                    (process.ppid() == current_process.pid) or
                    (process.ppid() == current_process.ppid())):
                    continue
                # 使用中のポートに追加
                if conn.laddr is not None:
                    used_ports.append(cast(Any, conn.laddr).port)
        # リッスンポートと同じポートが使われていたら、エラーを表示する
        # Akebi HTTPS Server のリッスンポートと Uvicorn のリッスンポートの両方をチェック
        if port in used_ports:
            raise ValueError(
                f'ポート {port} は他のプロセスで使われているため、KonomiTV を起動できません。\n'
                f'重複して KonomiTV を起動していないか、他のソフトでポート {port} を使っていないかを確認してください。'
            )
        if (port + 10) in used_ports:
            raise ValueError(
                f'ポート {port + 10} ({port} + 10) は他のプロセスで使われているため、KonomiTV を起動できません。\n'
                f'重複して KonomiTV を起動していないか、他のソフトでポート {port + 10} を使っていないかを確認してください。'
            )
        return port

class _ServerSettingsTV(BaseModel):
    max_alive_time: PositiveInt
    debug_mode_ts_path: FilePath | None

class _ServerSettingsCapture(BaseModel):
    upload_folder: DirectoryPath

class _ServerSettingsTwitter(BaseModel):
    consumer_key: str | None
    consumer_secret: str | None

class ServerSettings(BaseModel):
    general: _ServerSettingsGeneral
    server: _ServerSettingsServer
    tv: _ServerSettingsTV
    capture: _ServerSettingsCapture
    twitter: _ServerSettingsTwitter

    @classmethod
    def unverified(cls, config_dict: dict[str, dict[str, Any]]) -> 'ServerSettings':
        """
        バリデーションをスキップして ServerSettings オブジェクトを生成する
        ref: https://github.com/pydantic/pydantic/issues/897
        """

        return ServerSettings.construct(
            general = _ServerSettingsGeneral.construct(**config_dict['general']),
            server = _ServerSettingsServer.construct(**config_dict['server']),
            tv = _ServerSettingsTV.construct(**config_dict['tv']),
            capture = _ServerSettingsCapture.construct(**config_dict['capture']),
            twitter = _ServerSettingsTwitter.construct(**config_dict['twitter']),
        )


# サーバー設定データと読み込み・保存用の関数
# _CONFIG には config.yaml から読み込んだ KonomiTV サーバーの設定データが保持される
# _CONFIG には直接アクセスせず、Config() 関数を通してアクセスする

_CONFIG: ServerSettings | None = None
_CONFIG_YAML_PATH = BASE_DIR.parent / 'config.yaml'
_DOCKER_PATH_PREFIX = '/host-rootfs'


def LoadConfig(bypass_validation: bool = False) -> ServerSettings:
    """
    config.yaml からサーバー設定データのロードとバリデーションを行い、グローバル変数に格納する
    基本 KonomiTV サーバー起動時に一度だけ呼び出されるが、自動リロードモードやマルチプロセス実行時にも呼び出される
    いずれの場合でも、1プロセス内で複数回呼び出されることはない

    Args:
        bypass_validation (bool): バリデーションをスキップするかどうか

    Raises:
        SystemExit: 設定ファイルが配置されていなかったり、設定内容が不正な場合はサーバーを起動できないため、プロセスを終了する

    Returns:
        ServerSettings: 読み込んだサーバー設定データ
    """

    global _CONFIG, _CONFIG_YAML_PATH, _DOCKER_PATH_PREFIX
    assert _CONFIG is None, 'LoadConfig() は既に呼び出されています。'

    # 循環参照を避けるために遅延インポート
    from app.utils import GetPlatformEnvironment
    from app.utils import Logging

    # 設定ファイルが配置されていない場合、エラーを表示して終了する
    if Path.exists(_CONFIG_YAML_PATH) is False:
        Logging.error('設定ファイルが配置されていないため、KonomiTV を起動できません。')
        Logging.error('config.example.yaml を config.yaml にコピーし、お使いの環境に合わせて編集してください。')
        sys.exit(1)

    # 設定ファイルからサーバー設定をロードする
    try:
        with open(_CONFIG_YAML_PATH, encoding='utf-8') as file:
            config_raw = ruamel.yaml.YAML().load(file)
            if config_raw is None:
                Logging.error('設定ファイルが空のため、KonomiTV を起動できません。')
                Logging.error('config.example.yaml を config.yaml にコピーし、お使いの環境に合わせて編集してください。')
                sys.exit(1)
        config_dict: dict[str, dict[str, Any]] = dict(config_raw)
    except Exception as error:
        Logging.error('設定ファイルのロード中にエラーが発生したため、KonomiTV を起動できません。')
        Logging.error(f'{type(error).__name__}: {error}')
        sys.exit(1)

    try:
        # EDCB / Mirakurun の URL の末尾のスラッシュをなしに統一
        ## これをやっておかないと Mirakurun の URL の末尾にスラッシュが入ってきた場合に接続に失敗する
        ## EDCB に関しては統一する必要はないが、念のため
        config_dict['general']['edcb_url'] = config_dict['general']['edcb_url'].rstrip('/')
        config_dict['general']['mirakurun_url'] = config_dict['general']['mirakurun_url'].rstrip('/')

        # Docker 上で実行されているとき、サーバー設定のうちパス指定の項目に Docker 環境向けの Prefix (/host-rootfs) を付ける
        ## /host-rootfs (docker-compose.yaml で定義) を通してホストマシンのファイルシステムにアクセスできる
        if GetPlatformEnvironment() == 'Linux-Docker':
            config_dict['capture']['upload_folder'] = _DOCKER_PATH_PREFIX + config_dict['capture']['upload_folder']
            if type(config_dict['tv']['debug_mode_ts_path']) is str:
                config_dict['tv']['debug_mode_ts_path'] = _DOCKER_PATH_PREFIX + config_dict['tv']['debug_mode_ts_path']
    except KeyError:
        pass  # config.yaml の記述が不正な場合は何もしない（どっちみち後のバリデーション処理で弾かれる）

    # サーバー設定のバリデーションを実行
    if bypass_validation is False:
        try:
            _CONFIG = ServerSettings(**cast(Any, config_dict))
            Logging.debug_simple('Server settings loaded.')
        except ValidationError as error:

            # エラーのうちどれか一つでもカスタムバリデーターからのエラーだった場合
            ## カスタムバリデーターからのエラーメッセージかどうかは "。" がメッセージに含まれているかで判定する
            custom_error = False
            for error_message in error.errors():
                if '。' in error_message['msg']:
                    custom_error = True
                    for message in error_message['msg'].split('\n'):
                        Logging.error(message)
            if custom_error is True:
                sys.exit(1)

            # それ以外のバリデーションエラー
            Logging.error('設定内容が不正なため、KonomiTV を起動できません。')
            Logging.error('以下のエラーメッセージを参考に、config.yaml の記述が正しいかを確認してください。')
            Logging.error(error)
            sys.exit(1)
    else:
        _CONFIG = ServerSettings.unverified(config_dict)
        Logging.debug_simple('Server settings loaded (bypassed validation).')

    return _CONFIG


def SaveConfig(config: ServerSettings) -> None:
    """
    変更されたサーバー設定データを、コメントやフォーマットを保持した形で config.yaml に書き込む
    config.yaml のすべてを上書きするのではなく、具体的な値が記述されている部分のみ正規表現で置換している
    この関数は _CONFIG を更新しないため、設定変更を反映するにはサーバーを再起動する必要がある
    (仮に _CONFIG を更新するよう実装しても、すでに Config() から取得した値を使って実行されている処理は更新できない)

    Args:
        config (ServerSettings): 変更されたサーバー設定データ
    """

    global _CONFIG_YAML_PATH, _DOCKER_PATH_PREFIX

    # 循環参照を避けるために遅延インポート
    from app.utils import GetPlatformEnvironment

    # ServerSettings の Pydantic モデルを辞書に変換
    config_dict = config.dict()

    # Docker 上で実行されているとき、サーバー設定のうちパス指定の項目に付与されている Docker 環境向けの Prefix (/host-rootfs) を外す
    ## LoadConfig() で実行されている処理と逆の処理を行う
    if GetPlatformEnvironment() == 'Linux-Docker':
        config_dict['capture']['upload_folder'] = str(config_dict['capture']['upload_folder']).replace(_DOCKER_PATH_PREFIX, '')
        if type(config_dict['tv']['debug_mode_ts_path']) is str or config_dict['tv']['debug_mode_ts_path'] is Path:
            config_dict['tv']['debug_mode_ts_path'] = str(config_dict['tv']['debug_mode_ts_path']).replace(_DOCKER_PATH_PREFIX, '')

    # 現在の config.yaml の内容を行ごとに取得
    current_lines: list[str]
    with open(_CONFIG_YAML_PATH, mode='r', encoding='utf-8') as file:
        current_lines = file.readlines()

    # 新しく作成する config.yaml の内容が入るリスト
    ## このリストに格納された値を、最後に文字列として繋げて書き込む
    new_lines: list[str] = []

    current_parent_key: str = ''
    in_list: bool = False
    list_key: str = ''

    for current_line in current_lines:
        parent_key_match_pattern = r'^ {4}(?P<key>\'.*?\'|\".*?\"): \{$'
        parent_key_match = re.match(parent_key_match_pattern, current_line)

        if parent_key_match is not None:
            parent_key_match_data = parent_key_match.groupdict()
            current_parent_key = parent_key_match_data['key'].replace('"', '').replace('\'', '')
            new_lines.append(current_line)
            continue

        list_start_pattern = r'^ {8}(?P<key>\'.*?\'|\".*?\"): \[\s*(#.*)?$'
        list_start_match = re.match(list_start_pattern, current_line)

        if list_start_match is not None:
            list_start_match_data = list_start_match.groupdict()
            list_key = list_start_match_data['key'].replace('"', '').replace('\'', '')
            in_list = True

            if list_key in config_dict[current_parent_key]:
                new_list = ",\n".join([f"        '{item}'" for item in config_dict[current_parent_key][list_key]])
                new_lines.append(f"    '{list_key}': [\n{new_list}\n    ],\n")
            else:
                new_lines.append(current_line)
            continue

        list_end_pattern = r'^ {8}\],\s*(#.*)?$'
        list_end_match = re.match(list_end_pattern, current_line)

        if list_end_match is not None:
            in_list = False
            continue

        if in_list:
            continue

        key_value_match_pattern = r'^ {8}(?P<key>\'.*?\'|\".*?\"): (?P<value>[0-9\.]+|true|false|null|\'.*?\'|\".*?\"),$'
        key_value_match = re.match(key_value_match_pattern, current_line)

        if key_value_match is not None:
            key_value_match_data = key_value_match.groupdict()
            key = key_value_match_data['key'].replace('"', '').replace('\'', '')

            if key in config_dict[current_parent_key]:
                value = config_dict[current_parent_key][key]

                if type(value) is int or type(value) is float:
                    value_real = str(value)
                    if value_real.endswith('.0'):
                        value_real = value_real[:-2]
                elif value is True:
                    value_real = 'true'
                elif value is False:
                    value_real = 'false'
                elif value is None:
                    value_real = 'null'
                else:
                    value_real = f"'{value}'"
                value_real = value_real.replace('\\', '\\\\')

                new_line = re.sub(key_value_match_pattern, r'        \g<key>: ' + value_real + ',', current_line)
                new_lines.append(new_line)
                continue

        new_lines.append(current_line)

    # 置換が終わったので、config.yaml に書き込む
    ## リスト内の各要素にはすでに改行コードが含まれているので、空文字で join() するだけで OK
    with open(_CONFIG_YAML_PATH, mode='w', encoding='utf-8') as file:
        file.write(''.join(new_lines))


def Config() -> ServerSettings:
    """
    LoadConfig() でグローバル変数に格納したサーバー設定データを取得する
    この関数は、LoadConfig() を実行した後に呼び出さなければならない

    Returns:
        ServerSettings: サーバー設定データ
    """

    global _CONFIG
    assert _CONFIG is not None, 'サーバー設定データが初期化されていません。'
    return _CONFIG


def GetServerPort() -> int:
    """
    サーバーのポート番号を返す (KonomiTV-Service.py でポート番号を取得するために使用)
    KonomiTV-Service.py ではバリデーションは行いたくないので、Pydantic には通さずに config.yaml から直接ロードする

    Returns:
        int: サーバーのポート番号
    """

    try:

        # 設定ファイルからサーバー設定をロードし、ポート番号だけを返す
        with open(_CONFIG_YAML_PATH, encoding='utf-8') as file:
            config_dict: dict[str, dict[str, Any]] = dict(ruamel.yaml.YAML().load(file))
        return config_dict['server']['port']

    # 処理中にエラーが発生した (config.yaml が存在しない・フォーマットが不正など) 場合は、デフォルトのポート番号を返す
    except Exception:
        return 7000
