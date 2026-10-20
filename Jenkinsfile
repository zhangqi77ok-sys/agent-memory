// agent-memory CI/CD（Jenkins 在固定 IP 的部署机上，见 docs/adr/0002、docs/deploy.md）
// 凭据（Jenkins Credentials，仓库里只出现 ID）：
//   deploy-ssh  SSH 私钥，登录部署机 deploy 用户
//   deploy-env  Secret file，部署机 /opt/agent-memory/.env 的内容
//   ghcr-token  用户名+PAT（write:packages），可选；不存在时跳过推送，部署本机构建的镜像
// Jenkins 全局环境变量：DEPLOY_HOST（部署机地址，不进仓库）、DEPLOY_USER（默认 deploy）
pipeline {
  agent any
  options { buildDiscarder(logRotator(numToKeepStr: '10')); timeout(time: 30, unit: 'MINUTES'); timestamps() }
  parameters {
    string(name: 'DEPLOY_BRANCH', defaultValue: 'main', description: '只有这个分支会执行 构建/部署/冒烟')
    string(name: 'SMOKE_PATH', defaultValue: '/readyz', description: '冒烟路径；回滚演练时改成不存在的路径')
  }
  environment {
    GHCR_IMAGE = 'ghcr.io/zhangqi77ok-sys/agent-memory'
    LOCAL_IMAGE = 'agent-memory'
    APP_DIR = '/opt/agent-memory'
    CI_NET = 'jenkins_default'   // Jenkins 容器所在网络，CI 临时 Postgres 也挂这里
  }
  stages {
    stage('Init') {
      steps {
        script {
          env.BR = (env.BRANCH_NAME ?: env.GIT_BRANCH ?: '').replaceFirst(/^origin\//, '')
          env.DO_DEPLOY = (env.BR == params.DEPLOY_BRANCH) ? 'true' : 'false'
          env.TAG = "${env.GIT_COMMIT.take(12)}-b${env.BUILD_NUMBER}"
          env.DEPLOY_USER = env.DEPLOY_USER ?: 'deploy'
          if (env.DO_DEPLOY == 'true' && !env.DEPLOY_HOST) { error 'DEPLOY_HOST 未配置（Jenkins 系统设置→全局属性→环境变量）' }
          echo "branch=${env.BR} deploy=${env.DO_DEPLOY} tag=${env.TAG}"
        }
      }
    }
    stage('Secrets scan') {
      // Jenkins 跑在容器里，宿主 docker 看不到容器内路径，用 --volumes-from 共享 jenkins_home
      steps { sh 'docker run --rm --volumes-from "$(hostname)" zricethezav/gitleaks:v8.18.4 detect -s "$PWD" --no-banner' }
    }
    stage('Lint & Test') {
      steps {
        sh '''
          python3 -m venv .venv && . .venv/bin/activate
          pip install -q -e '.[dev]'
          ruff check app tests migrations
          pytest -q --junitxml=report-sqlite.xml
        '''
      }
      post { always { junit testResults: 'report-sqlite.xml', allowEmptyResults: false } }
    }
    stage('Test on Postgres') {
      // 行级权限、pgvector 索引、SKIP LOCKED 只有真实 Postgres 才能验证
      steps {
        sh '''
          PGC=amem-ci-$BUILD_NUMBER
          docker rm -f $PGC >/dev/null 2>&1 || true
          docker run -d --rm --name $PGC --network $CI_NET -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory \
            -e POSTGRES_DB=memory_test pgvector/pgvector:pg16
          trap "docker rm -f $PGC >/dev/null 2>&1 || true" EXIT
          ok=0; for i in $(seq 1 30); do docker exec $PGC pg_isready -U memory -d memory_test && ok=1 && break; sleep 2; done
          [ $ok = 1 ] || { echo 'CI Postgres 未就绪'; exit 1; }
          sleep 2
          . .venv/bin/activate
          TEST_DATABASE_URL=postgresql+psycopg://memory:memory@$PGC:5432/memory_test pytest -q -rs --junitxml=report-pg.xml
          python3 - <<'PY2'
import xml.etree.ElementTree as ET
r = ET.parse('report-pg.xml').getroot()
s = r if r.tag == 'testsuite' else r.find('testsuite')
tests, skipped = int(s.get('tests', 0)), int(s.get('skipped', 0))
print(f'[gate] postgres tests={tests} skipped={skipped}')
assert tests > 0 and skipped == 0, f'Postgres stage ran {tests} tests, {skipped} skipped'
PY2
        '''
      }
      post { always { junit testResults: 'report-pg.xml', allowEmptyResults: false } }
    }
    stage('Build image') {
      when { expression { env.DO_DEPLOY == 'true' } }
      steps { sh 'docker build -t $LOCAL_IMAGE:$TAG -t $GHCR_IMAGE:$TAG .' }
    }
    stage('Push GHCR') {
      when { expression { env.DO_DEPLOY == 'true' } }
      steps {
        script {
          try {
            withCredentials([usernamePassword(credentialsId: 'ghcr-token', usernameVariable: 'U', passwordVariable: 'P')]) {
              sh '''
                echo "$P" | docker login ghcr.io -u "$U" --password-stdin
                docker push $GHCR_IMAGE:$TAG
                docker tag $GHCR_IMAGE:$TAG $GHCR_IMAGE:latest && docker push $GHCR_IMAGE:latest
              '''
            }
            env.DEPLOY_IMAGE = env.GHCR_IMAGE
          } catch (org.jenkinsci.plugins.credentialsbinding.impl.CredentialNotFoundException e) {
            echo 'SKIPPED: no ghcr-token —— 不推送 GHCR，部署本机构建的镜像'
            env.DEPLOY_IMAGE = env.LOCAL_IMAGE
          }
        }
      }
    }
    stage('Deploy') {
      // 部署机就是 Jenkins 所在主机：通过 SSH 登录 deploy 用户执行 compose，路径都是宿主机路径
      when { expression { env.DO_DEPLOY == 'true' } }
      steps {
        withCredentials([file(credentialsId: 'deploy-env', variable: 'ENVF')]) {
          sshagent(['deploy-ssh']) {
            sh '''
              T="$DEPLOY_USER@$DEPLOY_HOST"; SSH="ssh -o StrictHostKeyChecking=accept-new"
              $SSH $T "mkdir -p $APP_DIR/initdb"
              scp -o StrictHostKeyChecking=accept-new -q deploy/docker-compose.yml deploy/backup.sh deploy/restore.sh $T:$APP_DIR/
              scp -q deploy/initdb/* $T:$APP_DIR/initdb/
              scp -q "$ENVF" $T:$APP_DIR/.env
              $SSH $T "cd $APP_DIR && chmod 600 .env && chmod +x backup.sh restore.sh &&
                echo $DEPLOY_IMAGE:$TAG > .last_tag.new &&
                IMAGE=$DEPLOY_IMAGE TAG=$TAG docker compose -p agent-memory up -d --remove-orphans"
            '''
          }
        }
      }
    }
    stage('Smoke') {
      when { expression { env.DO_DEPLOY == 'true' } }
      steps {
        sshagent(['deploy-ssh']) {
          sh '''
            ssh $DEPLOY_USER@$DEPLOY_HOST "cd $APP_DIR && P=\\$(grep ^API_PORT= .env | cut -d= -f2); P=\\${P:-8000};
              for i in \\$(seq 1 30); do c=\\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:\\$P$SMOKE_PATH); echo smoke \\$i \\$c; [ \\"\\$c\\" = 200 ] && exit 0; sleep 3; done; exit 1"
          '''
        }
      }
      post {
        success { sshagent(['deploy-ssh']) { sh 'ssh $DEPLOY_USER@$DEPLOY_HOST "cd $APP_DIR && mv .last_tag.new .last_tag && echo current=\\$(cat .last_tag)"' } }
        failure {
          sshagent(['deploy-ssh']) {
            sh '''
              ssh $DEPLOY_USER@$DEPLOY_HOST "cd $APP_DIR && rm -f .last_tag.new &&
                if [ ! -s .last_tag ]; then echo 'ROLLBACK: 没有上一个成功版本，无法回滚'; exit 1; fi &&
                PREV=\\$(cat .last_tag) && echo ROLLBACK to \\$PREV &&
                IMAGE=\\${PREV%:*} TAG=\\${PREV##*:} docker compose -p agent-memory up -d &&
                P=\\$(grep ^API_PORT= .env | cut -d= -f2); for i in \\$(seq 1 30); do c=\\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:\\${P:-8000}/readyz); [ \\"\\$c\\" = 200 ] && echo ROLLBACK OK readyz=200 && exit 0; sleep 3; done; exit 1"
            '''
          }
        }
      }
    }
  }
}
