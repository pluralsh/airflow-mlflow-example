"""
### Register Multiple Models with MLFlow
Choose from the results of multiple models for serving with MLFlow.

Uses a publicly avaliable Census dataset in Bigquery. 

Airflow can integrate with tools like MLFlow to streamline the model experimentation process. 
By using the automation and orchestration of Airflow together with MLflow's core concepts Data Scientists can standardize, share, and iterate over experiments more easily.


#### XCOM Backend
By default, Airflow stores all return values in XCom. However, this can introduce complexity, as users then have to consider the size of data they are returning. Futhermore, since XComs are stored in the Airflow database by default, intermediary data is not easily accessible by external systems.
By using an external XCom backend, users can easily push and pull all intermediary data generated in their DAG in GCS.
"""

from airflow.decorators import task, dag, task_group
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

from datetime import datetime

import logging
from airflow.utils.log.logging_mixin import LoggingMixin

import pandas as pd

from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb

import include.metrics as metrics
from include.grid_configs import models, params


@dag(
    start_date=datetime(2021, 1, 1),
    schedule_interval=None,
    catchup=False,
    doc_md=__doc__
)
def mlflow_multimodel_register_example():

    @task
    def load_data():
        """Pull Census data from Public BigQuery and save as Pandas dataframe in GCS bucket with XCom"""

        bq = BigQueryHook()
        sql = """
        SELECT * FROM `bigquery-public-data.ml_datasets.census_adult_income`
        """

        return bq.get_pandas_df(sql=sql, dialect='standard')


    @task
    def preprocessing(df: pd.DataFrame):
        """Clean Data and prepare for feature engineering
        
        Returns pandas dataframe via Xcom to GCS bucket.

        Keyword arguments:
        df -- Raw data pulled from BigQuery to be processed. 
        """

        df.dropna(inplace=True)
        df.drop_duplicates(inplace=True)

        # Clean Categorical Variables (strings)
        cols = df.columns
        for col in cols:
            if df.dtypes[col]=='object':
                df[col] =df[col].apply(lambda x: x.rstrip().lstrip())


        # Rename up '?' values as 'Unknown'
        df['workclass'] = df['workclass'].apply(lambda x: 'Unknown' if x == '?' else x)
        df['occupation'] = df['occupation'].apply(lambda x: 'Unknown' if x == '?' else x)
        df['native_country'] = df['native_country'].apply(lambda x: 'Unknown' if x == '?' else x)


        # Drop Extra/Unused Columns
        df.drop(columns=['education_num', 'relationship', 'functional_weight'], inplace=True)

        return df


    @task
    def feature_engineering(df: pd.DataFrame):
        """Feature engineering step
        
        Returns pandas dataframe via XCom to GCS bucket.

        Keyword arguments:
        df -- data from previous step pulled from BigQuery to be processed. 
        """
        
        # Onehot encoding 
        df = pd.get_dummies(df, prefix='workclass', columns=['workclass'])
        df = pd.get_dummies(df, prefix='education', columns=['education'])
        df = pd.get_dummies(df, prefix='occupation', columns=['occupation'])
        df = pd.get_dummies(df, prefix='race', columns=['race'])
        df = pd.get_dummies(df, prefix='sex', columns=['sex'])
        df = pd.get_dummies(df, prefix='income_bracket', columns=['income_bracket'])
        df = pd.get_dummies(df, prefix='native_country', columns=['native_country'])

        # Bin Ages
        df['age_bins'] = pd.cut(x=df['age'], bins=[16,29,39,49,59,100], labels=[1, 2, 3, 4, 5])

        # Dependent Variable
        df['never_married'] = df['marital_status'].apply(lambda x: 1 if x == 'Never-married' else 0) 

        # Drop redundant column
        df.drop(columns=['income_bracket_<=50K', 'marital_status', 'age'], inplace=True)

        return df


    @task_group(group_id='grid_search_cv')
    def grid_search_cv(features: pd.DataFrame):
        """Train and validate model using a grid search for the optimal parameter values and a five fold cross validation.
        
        Returns accuracy score via XCom to GCS bucket.

        Keyword arguments:
        df -- data from previous step pulled from BigQuery to be processed. 
        """

        tasks = []

        for k in models:
            @task(task_id=k, multiple_outputs=True)
            def train(df: pd.DataFrame, model_type=k,model=models[k], grid_params=params[k], **kwargs):

                import mlflow

                mlflow.set_tracking_uri('http://mlflow.mlflow.svc')
                try:
                    # Creating an experiment
                    mlflow.create_experiment('census_prediction')
                except:
                    pass
                # Setting the environment with the created experiment
                mlflow.set_experiment('census_prediction')

                mlflow.sklearn.autolog()
                mlflow.lightgbm.autolog()

                y = df['never_married']
                X = df.drop(columns=['never_married'])

                X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=55, stratify=y)

                grid_search = GridSearchCV(model, param_grid=grid_params, verbose=1, cv=5, n_jobs=-1)

                with mlflow.start_run(run_name=f'{model_type}_{kwargs["run_id"]}') as run:

                    logging.info('Performing Gridsearch')
                    grid_search.fit(X_train, y_train)

                    logging.info(f'Best Parameters\n{grid_search.best_params_}')
                    best_params = grid_search.best_params_

                    if model_type == 'lgbm':

                        train_set = lgb.Dataset(X_train, label=y_train)
                        test_set = lgb.Dataset(X_test, label=y_test)

                        best_params['metric'] = ['auc', 'binary_logloss']

                        logging.info('Training model with best parameters')
                        clf = lgb.train(
                            train_set=train_set,
                            valid_sets=[train_set, test_set],
                            valid_names=['train', 'validation'],
                            params=best_params,
                            early_stopping_rounds=5
                        )

                    else:
                        logging.info('Training model with best parameters')
                        clf = LogisticRegression(penalty=best_params['penalty'], C=best_params['C'], solver=best_params['solver']).fit(X_train, y_train)

                    y_pred_class = metrics.test(clf, X_test)

                    # Log Classfication Report, Confustion Matrix, and ROC Curve
                    metrics.log_all_eval_metrics(y_test, y_pred_class)

                    return {'run_id': run.info.run_id, 'model_type': model_type}
                
            run_id = train(features)
            tasks.append(run_id)

        return tasks


    @task(multiple_outputs=True)
    def get_best_model(run_ids: list):

        import mlflow

        mlflow.set_tracking_uri('http://mlflow.mlflow.svc')
        try:
            # Creating an experiment
            mlflow.create_experiment('census_prediction')
        except:
            pass
        # Setting the environment with the created experiment
        mlflow.set_experiment('census_prediction')

        logging.info(run_ids)

        best = {
            'run_id': '',
            'model': '',
            'auc_score': 0,
            'accuracy': 0
        }
        
        for run_id in run_ids:

            logging.info(run_id['run_id'])

            run_data = mlflow.get_run(run_id['run_id']).data.to_dictionary()
            auc_score = run_data['metrics']['test_auc_score']
            accuracy = run_data['metrics']['accuracy']

            logging.info(f'AUC Score: {auc_score}')
            logging.info(f'Accuracy: {accuracy}')

            if auc_score > best['auc_score']:
                best['auc_score'] = auc_score
                best['accuracy'] = accuracy
                best['run_id'] = run_id['run_id']
                best['model'] = run_id['model_type']
            elif auc_score == best['auc_score'] and accuracy > best['accuracy']:
                best['auc_score'] = auc_score
                best['accuracy'] = accuracy
                best['run_id'] = run_id['run_id']
                best['model'] = run_id['model_type']
            else:
                pass
        
        logging.info(best)

        best_params = {}
        best_run = mlflow.get_run(best['run_id']).data.to_dictionary()
        best_run = best_run['params']

        for k in best_run:
            if k.startswith('best_'):

                if '.' in best_run[k]:
                    best_params[k[len('best_'):]] = float(best_run[k])
                elif best_run[k].isdigit():
                    best_params[k[len('best_'):]] = int(best_run[k])
                else:
                    best_params[k[len('best_'):]] = best_run[k]


        logging.info(best_params)

        return {'params': best_params, 'model_type': best['model']}


    @task
    def build_best_model(model_params: dict, features: pd.DataFrame, **kwargs):

        import mlflow

        mlflow.set_tracking_uri('http://mlflow.mlflow.svc')
        try:
            # Creating an experiment
            mlflow.create_experiment('census_prediction')
        except:
            pass
        # Setting the environment with the created experiment
        mlflow.set_experiment('census_prediction')

        logging.info(model_params)

        y = features['never_married']
        X = features.drop(columns=['never_married'])

        train_set = lgb.Dataset(X, label=y)

        with mlflow.start_run(run_name=f'{model_params["model_type"]}_{kwargs["run_id"]}_best') as run:

            if model_params['model_type'] == 'lgbm':
                
                base_params = {'objective':'binary', 'metric':['auc', 'binary_logloss'], 'boosting_type':'gbdt'}
                all_params = {**base_params, **model_params['params']}

                lgb.train(
                    train_set=train_set,
                    params=all_params
                )

            else:
                base_params = {'max_iter': 500}
                all_params = {**base_params, **model_params['params']}

                LogisticRegression(params=all_params)  
        
            return run.info.run_id


    @task
    def register_model(model_run_id: str):
        import mlflow

        mlflow.set_tracking_uri('http://mlflow.mlflow.svc')
        try:
            # Creating an experiment
            mlflow.create_experiment('census_prediction')
        except:
            pass
        # Setting the environment with the created experiment
        mlflow.set_experiment('census_prediction')
        
        mv = mlflow.register_model(f'runs:/{model_run_id}/model', 'census_pred',)

        logging.info(f'Name: {mv.name}')
        logging.info(f'Version: {mv.version}')

        client = mlflow.tracking.MlflowClient()

        client.transition_model_version_stage(
            name=mv.name,
            version=mv.version,
            stage="Staging")



    df = load_data()
    clean_data = preprocessing(df)
    features = feature_engineering(clean_data)
    run_ids = grid_search_cv(features)
    best_model_params = get_best_model(run_ids)
    final_model_run_id = build_best_model(best_model_params, features)
    register_model(final_model_run_id)

    
dag = mlflow_multimodel_register_example()
