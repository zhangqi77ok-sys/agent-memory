// agent-memory CI/CD：proxy-3 上的 Jenkins
// 需要的 Credentials：ghcr-token（用户名+PAT, write:packages）、proxy4-ssh（SSH key）
// Jenkins→proxy-4 连通方式待实测；若 SSH 不通，改为 proxy-4 上 Jenkins agent（label 'proxy-4'）
pipeline {
  agent any
  options { buildDiscarder(logRotator(numToKeepStr: '10')); timeout(time: 30, unit: 'MINUTES') }
  environment {
    IMAGE = 'ghcr.io/zhangqi77ok-sys/agent-memory'
    TAG = "${env.GIT_COMMIT?.take(12) ?: 'dev'}"
    DEPLOY_HOST = 'proxy-4'
  }
  post { always { junit allowEmptyResults: true, testResults: 'report-*.xml' } }
  stages {
    stage('Secrets scan') {
      steps { sh 'docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.18.4 detect -s /repo --no-banner' }
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
    }
    stage('Test on Postgres') {
      // 行级权限、pgvector 索引、SKIP LOCKED 只有真实 Postgres 才能验证
      steps {
        sh '''
          PGC=amem-ci-$BUILD_NUMBER
          docker run -d --rm --name $PGC -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory -e POSTGRES_DB=memory_test \
            -p 127.0.0.1::5432 pgvector/pgvector:pg16
          trap "docker rm -f $PGC >/dev/null 2>&1 || true" EXIT
          for i in $(seq 1 30); do docker exec $PGC pg_isready -U memory && break; sleep 2; done
          PORT=$(docker port $PGC 5432/tcp | head -1 | cut -d: -f2)
          . .venv/bin/activate
          TEST_DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:$PORT/memory_test pytest -q --junitxml=report-pg.xml
        '''
      }
    }
    stage('Build & Push') {
      when { branch 'main' }
      steps {
        withCredentials([usernamePassword(credentialsId: 'ghcr-token', usernameVariable: 'U', passwordVariable: 'P')]) {
          sh '''
            echo "$P" | docker login ghcr.io -u "$U" --password-stdin
            docker build -t $IMAGE:$TAG -t $IMAGE:latest .
            docker push $IMAGE:$TAG && docker push $IMAGE:latest
          '''
        }
      }
    }
    stage('Deploy') {
      when { branch 'main' }
      steps {
        sshagent(['proxy4-ssh']) {
          sh '''
            ssh -o StrictHostKeyChecking=accept-new $DEPLOY_HOST "
              cd /opt/agent-memory && git pull -q &&
              echo $TAG > .last_tag.new &&
              TAG=$TAG docker compose -f deploy/docker-compose.yml pull api &&
              TAG=$TAG docker compose -f deploy/docker-compose.yml up -d api"
          '''
        }
      }
    }
    stage('Smoke') {
      when { branch 'main' }
      steps {
        sshagent(['proxy4-ssh']) {
          sh '''
            ssh $DEPLOY_HOST '
              for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8000/readyz && exit 0; sleep 3; done; exit 1'
          '''
        }
      }
      post {
        success { sshagent(['proxy4-ssh']) { sh 'ssh $DEPLOY_HOST "cd /opt/agent-memory && mv .last_tag.new .last_tag"' } }
        failure {
          sshagent(['proxy4-ssh']) {
            sh '''ssh $DEPLOY_HOST "cd /opt/agent-memory && PREV=\\$(cat .last_tag 2>/dev/null || echo latest) &&
                  TAG=\\$PREV docker compose -f deploy/docker-compose.yml up -d api"'''
          }
        }
      }
    }
  }
}
