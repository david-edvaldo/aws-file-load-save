import boto3
import pandas as pd
import pickle 
import json
import fsspec
import awswrangler as wr
import os    

from PIL import Image
from io import BytesIO
from typing import Any, Optional, Tuple

FILEEXTENS = {
    '.xls' :'excel',
    '.xlsx':'excel',
    '.xlsm':'excel',
    '.xlsb':'excel',
    '.csv' :'csv',
    '.parquet':'parquet',
    '.json':'json',
    '.pickle':'pickle'       
} 

for ext in Image.registered_extensions():
    FILEEXTENS[ext] = 'image'
    
class EnvConfig:
    ''''
    Class specifications credentials aws
        Args:
            access_key : (string) -- AWS access key ID.
            secret_key : (string) -- AWS secret access key.
            bucket_name: (string) -- The name of the bucket to S3.
            region_name: (string) -- Default region when creating new connections.
    '''
    
    def __init__(self, conn):
        self.__aws_args = conn
        self.set_variables()
            
    def set_variables(self):
        self.__aws_access_key_id = self.__aws_args.get('access_key')
        self.__aws_secret_access_key = self.__aws_args.get('secret_key')
        self.__aws_bucket = self.__aws_args.get('bucket_name')
        self.__region_name = self.__aws_args.get('region_name')

    def get_aws_access_key_id(self):
        return self.__aws_access_key_id
    
    def get_aws_secret_access_key(self):
        return self.__aws_secret_access_key
    
    def get_bucket_name(self):
        return self.__aws_bucket
    
    def get_region_name(self):
        return self.__region_name
    
class S3Config:
    
    def __init__(self, conn):
        environment = EnvConfig(conn)
        
        self.AWS_ACCESS_KEY_ID = environment.get_aws_access_key_id()
        self.AWS_SECRET_ACCESS_KEY = environment.get_aws_secret_access_key()
        self.AWS_REGION_NAME = environment.get_region_name()
        self.AWS_BUCKET = environment.get_bucket_name()
    
    def get_session(self):
        session = boto3.Session(
            aws_access_key_id=self.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=self.AWS_SECRET_ACCESS_KEY,
            region_name=self.AWS_REGION_NAME
            )
        return session
    
    def get_resource(self):
        session = self.get_session()
        return session.resource('s3')

    def get_client(self):
        session = self.get_session()
        return session.client("s3")
    
    def get_bucket(self):
        s3 = self.get_resource() 
        return s3.Bucket(self.AWS_BUCKET)
    
    def get_object_bucket(self, key_name, s3=None):
        data = s3.get_object(
            Bucket=self.AWS_BUCKET, 
            Key=key_name
        )['Body']

        return data

class FetchS3Data:
    
    def __init__(self, conn):
        self.s3 = S3Config(conn)
        self.client = self.s3.get_client()
        self.resource = self.s3.get_resource()
        
    def get_fs_path(self, uri:str) -> Tuple[fsspec.AbstractFileSystem, str]:
        '''
        Process an fsspec compatible URI into a FileSystem instance
        and the parsed "path".
                Args:
                    uri: A fsspec compatible URI

                Returns:
                    fs: A configured fsspec FileSystem instance
                    path: The path component of the URI
        '''

        opts = {
            'key':self.s3.AWS_ACCESS_KEY_ID, 
            'secret':self.s3.AWS_SECRET_ACCESS_KEY
        }
        
        fs, _, (path,) = fsspec.get_fs_token_paths(uri, storage_options=opts)
        
        return fs, path
    
    def make_parents(self, fs, path) -> Any:
        '''
        Make parent dirs of path
        '''
        parent = path.rsplit("/", 1)[0]
        fs.makedirs(parent, exist_ok=True)    
        
    def load_data(self, info):
            
        data = self.s3.get_object_bucket(info['path'], self.client)
        
        function = {
            'parquet':pd.read_parquet,
            'excel':pd.read_excel,
            'csv':pd.read_csv,
            'json':json.loads, 
            'pickle':pickle.loads,
            'image':Image.open
        }
        
        if info['format'] in ['parquet','excel','csv']:
            kwargs = info.get('pandas_args')
            return function.get(info['format'])(BytesIO(data.read()), **kwargs)
        
        elif info['format'] in ['pickle','json']:
            return function.get(info['format'])(data.read()) 
        
        elif info['format'] == 'image':                     
            return function.get(info['format'])(data) 
        
        else:
            raise ValueError(
                f"The object format for '{info['path']}' must be "
                f"instead of '_[{info['format']}]_'"
            )

    def save_data(self, obj, info):
        
        kwargs = info.get('pandas_args')
        
        if info['format'] in ['parquet','excel','csv']:
            
            with BytesIO() as output:
                getattr(obj, f"to_{info['format']}", None)(output, **kwargs)
                data = output.getvalue()

            self.resource.Object(
                self.s3.AWS_BUCKET,
                info['path']
            ).put(Body=data)
        
        
        elif info['format'] in ['pickle','json']:
                    
            info_path = f"s3://{self.s3.AWS_BUCKET}/{info['path']}"
                
            function = {
                'json':json.dumps,
                'pickle':pickle.dump
            }
            
            fs, path = self.get_fs_path(info_path)
            self.make_parents(fs, path)
            
            with fs.open(path, "wb") as f:
                
                if info['format'] == 'pickle':
                    return function.get(info['format'])(obj, f)
                
                elif info['format'] == 'json':
                    raw = function.get(info['format'])(obj)
                    f.write(str.encode(raw))

        else:
            raise ValueError(
                f"The object format for '{info['path']}' must be "
                f"instead of '_[{info['format']}]_'"
            )
        
        
class UtilitiesS3:
    '''
    Class to build the datasets
            Args:
                datasets : Dataframe with the data of each file that will be loaded in s3.
                conn     : Bucket access credentials in s3.
                    >>  access_key:  (string) -- AWS access key ID.
                    >>  secret_key:  (string) -- AWS secret access key.
                    >>  bucket_name: (string) -- The name of the bucket to S3.
                    >>  region_name: (string) -- Default region when creating new connections
    '''
       
    def __init__(self, conn: dict, datasets: Optional[dict] = {}):
        self.fd = FetchS3Data(conn)
        self.s3 = S3Config(conn)
        
        self.datasets = {}
        if datasets:
            for k, v in datasets.items():
                for _k, _v in v.items():
                    if _k in ['pandas_args','schema'] and len(_v) == 0:
                        datasets[k][_k] = {}
                
                _, file_format = os.path.splitext(v['path'])    
                datasets[k]['format'] = FILEEXTENS.get(file_format)
                        
            self.datasets = datasets

    def _cast_schema(self, df, schema):
        '''Filter and convert dtypes to the specified schema'''
        if len(schema) > 0:
            cols = []
            for case in schema:
                col = list(case.keys())[0]
                dtype = case[col]
                if dtype != "np.nan":
                    df[col] = df[col].astype(dtype)
                cols.append(col)

            return df[cols]
        
        else:
            return df
     
    def load_folder(
        self,
        base_uri: str, 
        ) -> dict:
        '''
        Load a object in all folder.
        Types de object ['parquet', 'excel', 'csv', 'json', 'pickle', 'image']
                Args:
                    base_uri : Base path where the files are.
        '''
        
        self.datasets = {}
        self.file_name = []
        fpath = f"s3://{self.s3.AWS_BUCKET}/{base_uri}"
        
        _all_name = wr.s3.list_objects(fpath, boto3_session=self.s3.get_session())
        if not _all_name:
                raise ValueError(
            f"An error occurred (NoSuchKey) when calling the GetObject "
            f"operation: The specified key does not exist."
        )
                
        i=0    
        for f in _all_name:
            _, file_format = os.path.splitext(f) 
            _ext = FILEEXTENS.get(file_format)   
            self.file_name.append(f'{_ext}_{i}')
            
            self.datasets[f'{_ext}_{i}']={
                "path":f'{base_uri}{f.split("/")[-1]}',
                "format":_ext,
                "pandas_args":{},
                "schema":{}
            }
            i+=1
            
        self.cast_schema = False 
        self.pandas_args = {}   
        
        return self.flow_load()
        
                          
    def load_file(
        self,
        file_name: list,        
        cast_schema: Optional[bool] = False, 
        **pandas_args        
        ) -> dict:
        '''
        Load a object must be "pickable".
        Types de object ['parquet', 'excel', 'csv', 'json', 'pickle', 'image']
                Args:
                    file_name   : List of file names.
                    cast_schema : Option to cast and filter the dataset to the specified schema.
                    pandas_args : Extra arguments to pass to the "to_" function.
        '''
                        
        self.file_name = file_name if isinstance(file_name, list) else [file_name]
        
        self.cast_schema = cast_schema
        self.pandas_args = pandas_args
        
        return self.flow_load()
        
    def flow_load(self):
        _datasets = {}
        for f in self.file_name:
            if f not in self.datasets:
                raise ValueError(
                    f"The object '{f}' must be in list "
                    f"Indicated by the user"
                )
                
            info = self.datasets.get(f)
            info['pandas_args'].update(self.pandas_args)
            
            if info['format'] in ['excel','csv','parquet']:
                
                df = self.fd.load_data(info)
                _datasets[f] = (self._cast_schema(df, info['schema']) 
                if self.cast_schema 
                else df
                )
            
            elif info['format'] in ['pickle','json','image']:
                _datasets[f] = self.fd.load_data(info)
            
            else:
                raise ValueError(
                    f"The object format for '{info['path']}' must be "
                    f"instead of '_[{info['format']}]_'"
                )
        
        return _datasets
    
    
    def save_file(
        self,
        obj: Any,
        file_name: str,        
        cast_schema: Optional[bool] = False,  
        **pandas_args        
        ):
        '''
        Save a object must be "pickable".
        Types de object ['parquet', 'excel', 'csv', 'json', 'pickle']
                Args:
                    obj         : The object to save. Must be "pickable".
                    file_name   : List of file names.
                    cast_schema : Option to cast and filter the dataset to the specified schema.
                    pandas_args : Extra arguments to pass to the "to_" function.
        '''
          
        if not isinstance(file_name, str):
            raise ValueError(
                f"Attribute 'file_name' needed only #1 filename: str"
            )
            
        _f = file_name 
        info = self.datasets.get(_f)
        info['pandas_args'].update(pandas_args)
        
        _obj = (self._cast_schema(obj, info['schema']) 
        if cast_schema 
        else obj
        )
        
        self.fd.save_data(_obj, info)      
