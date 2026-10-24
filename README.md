# CoursewareHub-LC_deploy

CoursewareHubの構築notebook

AWS上に、CoursewareHubの運用管理用の[OperationHub](https://github.com/NII-cloud-operation/OperationHub)と、CoursewareHubを構築するnotebookです。

## 構築方法

最初にDocker Engineとdocker-composeがインストールされた環境が必要です。

以下のような流れで構築します。

1. Notebookサーバーの起動
1. そのNotebookサーバーを使用して、OperationHubを構築
1. 構築したOperationHubを使用して、CourseareHubを構築


### Notebookサーバーの起動

このリポジトリをcloneし、以下のように、Notebookサーバーを起動します。

sudoの有無は環境に応じて読み替えてください。

    $ git clone https://github.com/NII-cloud-operation/CoursewareHub-LC_deploy
    $ cd CoursewareHub-LC_deploy
    $ sudo docker-compose up --build

以下のようなログが出力されます。

```
jupyter_1  | [C 08:07:38.436 NotebookApp]
jupyter_1  |
jupyter_1  |     To access the notebook, open this file in a browser:
jupyter_1  |         file:///home/jovyan/.local/share/jupyter/runtime/nbserver-7-open.html
jupyter_1  |     Or copy and paste one of these URLs:
jupyter_1  |         http://e6fe59168280:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
jupyter_1  |      or http://127.0.0.1:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

http://127.0.0.1:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx にアクセスします。

ローカルホスト以外で起動した場合は、ホスト名は読み替えてください。

### OperationHubの構築

Notebookサーバーにログインしたら、「D01_OperationHubをAWSに構築.ipynb」を実行し、OperationHubを構築します。

### CoursewareHubの構築

構築したOperationHubにログインし、以下のNotebookを順に実行して、CoursewareHubを構築します。

1. D01_CoursewareHub用VMをAWSに作成.ipynb
1. D02_CoursewareHubインベントリの準備.ipynb
1. D03_NFSの準備.ipynb
1. D04_Docker Swarmの準備.ipynb
1. D05_CoursewareHubの準備.ipynb

「D06_CoursewareHubコンテンツの配備.ipynb」は、構築したCoursewareHub上で実行してください。

## E2Eテスト（開発用）

`feature/e2e-test`ブランチでは、GitHub Actionsの
[`Container Swarm and Notebook smoke`](.github/workflows/e2e-container-smoke.yml)
で構築と動作確認を実行します。ワークフローの実行先は
`yacchin1205/CoursewareHub-LC_deploy`に限定しています。

**構築Notebookが完走したことと、構築されたCoursewareHubが動作することは別に判定します。**
D01〜D06は構築手順です。構築後の受け入れテストは
[`tests/e2e/coursewarehub_ui.py`](tests/e2e/coursewarehub_ui.py)に分けています。

試験は次の流れで行います。

1. runner内にSSH・systemdを持つRocky Linux 9コンテナを2台用意します。
2. オンプレミス用D01とD02を実行し、接続とInventoryを準備します。
3. D03から既存Ansible roleで実際のNFSを設定し、読み書きとコンテナ再起動後の再マウントを確認します。
4. D04自身にDocker導入・systemd設定・Compose確認・Swarm構築を実行させ、検証用サービスでoverlay通信とNFS共有を確認します。
5. D05でCoursewareHub本体とローカル管理者を構築します。
6. Playwrightでログインし、ユーザーNotebookを起動して、作成した非root UIDでPythonセルを実行・保存します。
7. そのユーザーNotebook環境内でD06を実行し、配備した管理コンテンツにアクセスできることを確認します。
8. ユーザーサーバーを停止・再起動し、保存したファイルとNotebookが残っていることを確認します。

試験対象はCoursewareHubの`2024.03`イメージと、従来の
`CoursewareUserSpawner`による単一ユーザー方式です。この方式を追加設定で選択し、
イメージ既定のrepo2docker/registryサービスは無効にしています。
Dockerは`27.5.1`、Composeは`2.39.4`、ユーザーイメージは
`niicloudoperation/notebook:latest`を使用します。

コンテナはrunnerのカーネルを共有します。OS再起動の確認は実施せず、
コンテナ再起動後のNFS確認とは区別して記録します。
一部の事前check modeとSELinux設定もCIでは省略します。
AWS上のOperationHub/VM作成、最新`master`イメージ、Compose v1、
フェデレーション、LTI、repo2docker/registry、O01の更新は対象外です。

実行済みNotebook、構築段階の結果JSON、動作テストの結果JSON、画面、動画、
Playwright trace、NFS・systemd・DockerログはActionsのArtifactsに保存します。
失敗した場合は、構築途中の停止か、構築後の動作確認の失敗かをこれらから確認します。
SSH鍵と試験用パスワードは実行ごとに生成し、接続鍵は証跡のディレクトリには配置しません。

